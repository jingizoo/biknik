# #345 Acceptance-Evidence Ledger

## Status

**Audit/evidence-traceability document only.** This ledger inventories what
#345's nine acceptance boxes and required state matrix actually have behind
them today — merged, pending, or missing — so a reviewer can check
completion claims against production boundaries instead of inferring
completion from a green PR. It does not implement, fix, or waive anything.
No application code, tests, CI, `ROADMAP.md`, or existing protocol document
is changed by this PR.

**This document does not itself close any #345 acceptance box.** Where a row
below reads `Missing` or `Human-only / unperformed`, that remains true after
this document merges — the ledger records the gap, it does not fill it.

## Base and inspected heads

- **Base**: `main` at `49d662a95b163bea2c8303af6fa20cf429d14e7b` (merge of
  PR #351).
- **Active draft PRs inspected** (exact head SHA fetched via
  `refs/pull/<n>/head` and read directly, not assumed from the PR
  description):

  | PR | Title | Head SHA inspected | Base SHA | Mergeable state (checked live) |
  | --- | --- | --- | --- | --- |
  | [#347](https://github.com/jingizoo/biknik/pull/347) | Guided Setup hub: six summary-first workflow landings | `53fb8c2d119e064392416e824016c15d40ddf39a` | `4279ca4` (PR #348's merge — **behind** current `main`; #350/#351 landed after this branch's base) | `MERGEABLE` / `CLEAN`, full CI green on this head |
  | [#352](https://github.com/jingizoo/biknik/pull/352) | Cover login, public, error, and restricted shell states | `f2a0e554d84fef81131fa30600f6a2b40646228f` | `49d662a` (current `main`) | `MERGEABLE` / `CLEAN`, full CI green on this head |
  | [#353](https://github.com/jingizoo/biknik/pull/353) | Add seven-role destination and authorization matrix | `c682d3bb196e4ea5f4f53fc4046cb743ad404141` | `49d662a` (current `main`) | `MERGEABLE` / `CLEAN`, full CI green on this head |
  | [#354](https://github.com/jingizoo/biknik/pull/354) | Consolidate responsive breakpoints to 480/720/880/1040 (all four production stylesheets) | `b5e89612ed297f0df3e97889482a6f369daa63c1` | `49d662a` (current `main`) | **`CONFLICTING` / `DIRTY`** — see the stale-claim note under criterion 7 below |

- **Other sources read in full**: `ROADMAP.md` (current), issue
  [#345](https://github.com/jingizoo/biknik/issues/345) (body + all 7
  review/status comments), `docs/product/operator-ux-requirements.md`,
  `docs/product/moderated-operator-validation-protocol.md`,
  `docs/product/manual-keyboard-screenreader-validation-protocol.md`.

## How to read the status column

- **`Verified on main`** — the implementation is merged to `main` **and**
  the cited evidence (test/document path, or a direct repository read)
  covers the *entire* criterion, not a subset of it.
- **`Pending active PR`** — real evidence exists, checked and cited at an
  exact head SHA above, but it has not merged. **Non-authoritative** for
  #345's own merge gate until it lands on `main`.
- **`Missing`** — no implementation and no pending PR addresses it.
- **`Human-only / unperformed`** — the *procedure* to produce this evidence
  may be merged, but the evidence itself can only come from an actual human
  session/pass, and none has been run. Never marked `Verified` regardless of
  how complete the procedure document is.

---

## 1. Acceptance-criterion ledger

### Criterion 1 — "A first-time League Admin can identify and open the correct next incomplete setup step from one primary action."

| | |
| --- | --- |
| Required boundary/evidence | Home/Tasks landing computes the next incomplete workflow without the operator reading raw entity data, exposes exactly one primary action, and that action opens the correct destination directly (not merely the Setup tab). |
| Evidence merged to `main` | `renderSetupProgressCard()` (`backend/hockey_scheduler/web/static/app.js:563-712`) renders a single `data-setup-progress-action` primary button naming the real next-incomplete workflow in operator vocabulary (`next.label`/`next.primary_action`), backed by `get_setup_progress` (`backend/tests/test_setup_progress.py`). `goToSetupWorkflow(key)` (`app.js:762-828`) deep-links that one click straight into the correct destination for all six keys: a context-seeded, fail-closed create drawer for `league_season`/`teams`/`roster` (`app.js:775-814`, `contextSeededDrawerValues()` at `app.js:867+`), a focused Register-Team control for `participation` (`focusParticipationRegisterControl()`), the Ice Availability Builder for `facilities`, and the Import tab for `import`. Regression: `e2e/home-tasks-hub.js` (merged via PR #331, `16fe833`), asserting the deep-link lands on the real control (not just the tab) for each key, at desktop and 390×844. |
| Candidate evidence in active PR | None needed beyond what's merged — this criterion concerns the *action*, not the six-workflow *hub landing screen* (that's criterion 2). #347 (head `53fb8c2d1`) does not change `goToSetupWorkflow`'s already-merged behavior for the four keys carried over from PR #331; its own new code only affects **secondary/tertiary** actions inside the new landing (see criterion 2). |
| Remaining gap | None identified for the literal criterion text. |
| **Status** | **`Verified on main`** |

### Criterion 2 — "All six workflows are reachable through summary-first hub entries with existing capability preserved and Workflow 6 follows the approved optional contract."

| | |
| --- | --- |
| Required boundary/evidence | A hub index listing all six #204-named workflows, each with its own summary-first landing (not the old undifferentiated Setup mega-page as the *only* route); Workflow 6 ("Imports and onboarding") carries a third status distinct from done/todo, never `next`, never blocking. |
| Evidence merged to `main` | **None.** `grep -n "SETUP_WORKFLOWS\|renderSetupWorkflowLanding" app.js` on `main` returns zero matches — only the single `renderSetup(sv, hv, ov)` (`app.js:2838`) mega-page function exists, unchanged from before #345 opened. |
| Candidate evidence in active PR | #347 at head `53fb8c2d1` adds `SETUP_WORKFLOWS` (`app.js:2912`), `renderSetupHub()` (`app.js:2989`), and `renderSetupWorkflowLanding()` (`app.js:3015`) — a hub index of six cards plus one summary-first landing per workflow, each with exactly one `.act.primary`. The old Records/Hierarchy sub-views remain reachable via the existing segmented toggle (`renderSetup`'s `seg("hub"...)`/`seg("hierarchy"...)`/`seg("records"...)`), so no previously-reachable screen becomes unreachable — the PR's own regression (`e2e/setup-workflow-hub.js`) asserts this explicitly, plus a persisted two-Program/two-Season regression proving the batch-2 context-seeding blockers (reported on #345 against `ca8646b`/`7667c51`) are fixed. Workflow 6 carries `optional: true`, a distinct "Optional" badge (`app.js` landing markup), and its own explanatory copy, never competing with the five required workflows for the hub's `next` recommendation (unchanged from the already-merged `get_setup_progress` optional-status contract). Full CI green on `53fb8c2d1` (9/9 checks). |
| Remaining gap | Merge #347. Note also that #347's own PR body explicitly lists "the full state matrix" as **excluded** from this batch — the new landings do not yet carry per-card loading/empty/error/retry (see the state-matrix inventory, §2 below); criterion 2's literal text (reachability + optional contract) is satisfied by #347, but the *states* on those new landings are a separate, still-open gap tracked under criterion 5. |
| **Status** | **`Pending active PR`** (#347 @ `53fb8c2d1`) |

### Criterion 3 — "No previously reachable screen is unreachable under the seven-area IA."

| | |
| --- | --- |
| Required boundary/evidence | The proposed seven-area IA (Home/Tasks, Schedule, Teams & People, Facilities, Communications, Reports, Administration — `operator-ux-requirements.md` §2) replaces today's five nav groups (Home, Schedule, People, Operations, Admin Setup), with every one of today's reachable tabs mapped to a specific destination in the new IA. |
| Evidence merged to `main` | None. `grep -n "nav-group-label" index.html` on `main` still shows exactly the five original groups (`Home`, `Schedule`, `People`, `Operations`, `Admin Setup`) — unchanged. |
| Candidate evidence in active PR | **None.** Checked all four active PRs' file lists: #347 touches `app.js`/`styles.css`/e2e files only (no `index.html`); #352 touches `app.js`/`index.html`/`web.css` but only for shell-accessibility markup (skip link, titles, `role`/`aria-*`), not nav restructuring; #353 and #354 don't touch navigation at all. |
| Remaining gap | The seven-area IA itself — splitting "Admin Setup" into Facilities + Administration, promoting Users into Administration, etc. (`operator-ux-requirements.md` §2's full crosswalk table) — has **no implementation anywhere**, merged or pending. |
| **Status** | **`Missing`** |

### Criterion 4 — "Program/Season/League context filters changed screens correctly; Division remains local."

| | |
| --- | --- |
| Required boundary/evidence | Every screen in the new IA either filters by the selected Program/Season(/League) or documents a named, justified exception; the permanent "display only · screens not filtered" caption is removed or narrowed; League is promoted into the persistent context bar alongside Program/Season, while Division stays screen-local. |
| Evidence merged to `main` | The context switcher mechanism itself (`#context-switcher` wrapper / `#ctx-select` native `<select>`, `index.html:111-117`) is pre-existing (#159/#322/#323) and unchanged. |
| Candidate evidence in active PR | **None.** `grep -n "ctx-league\|League.*persistent"` finds nothing in any of the four PRs. The literal `<span class="ctx-unfiltered">display only · screens not filtered</span>` caption (`index.html:117`) is still present, unmodified, in every one of the four active heads. |
| Remaining gap | League promotion into the context bar, actual per-screen filtering wiring, and removal/narrowing of the "display only" caption are entirely unaddressed — not started in any merged or pending work. |
| **Status** | **`Missing`** |

### Criterion 5 — "Loading, empty, stale, error, retry, confirmation, optional, and complete states match the approved matrix."

| | |
| --- | --- |
| Required boundary/evidence | Per `operator-ux-requirements.md` §5, the full states matrix applies to Home/Tasks and each of the six Setup workflows (per-card error boundaries for hub-shaped screens, named empty states with the #311 recipe, stale-response guards, named-resource confirmation modals, and Workflow 6's distinct optional status). |
| Evidence merged to `main` | **Home/Tasks hub only**, fully: skeleton loading (`renderSetupProgressCard(_, _, true)`, `app.js:578-582`), per-card error + retry (`hadError` branch, `app.js:584-599`, `data-setup-progress-retry`), stale-response guard (`setupProgressFetchSeq`, `app.js:161,721,729`), success/complete (`progress.complete` branch, `app.js:602-640`), and a distinct optional badge for Workflow 6 (`app.js:643-649`). The six Setup workflows themselves still route through the single, pre-#345 `renderSetup()`/whole-pane `render()` skeleton (`app.js:6098`) and whole-pane `#retry-btn` (`app.js:6425`) — **not** per-card. |
| Candidate evidence in active PR | #347 (head `53fb8c2d1`) adds the landing *structure* (summary counts via `setupSummaryHtml()`) but, by its own PR body ("Explicitly NOT in this batch: ... the full state matrix"), does not add per-card loading/error/retry/stale-guard to the new landings — they inherit the same whole-pane behavior as today's mega-page. Its landing's own "empty" case is a bare `<div class="empty">Your role doesn't manage any setup workflows.…</div>` for a *role* with zero permitted workflows (`renderSetupHub`, `app.js`) — not the required per-workflow "No seasons yet" / "No teams yet" recipe from §5, which is not present anywhere in this PR either. |
| Remaining gap | Per-card loading/empty/stale/error/retry for all six new workflow landings; the confirmation-modal convention already exists for entity deletes in the pre-existing drill-in views (`records-delete`/`safe-destructive`/`division-delete-cleanup`/`registration-cleanup`/`player-lifecycle`/`destructive-surfaces` — all merged, pre-#345) but is not yet re-verified against the *new* landing entry points. See §2 below for the full per-screen inventory. |
| **Status** | **`Missing`** for the six Setup workflows' own landing-level states; **`Verified on main`** for Home/Tasks hub alone (this criterion is not satisfiable as a whole until the six-workflow gap closes) |

### Criterion 6 — "Player, Guardian, Official, Viewer, League Admin, Arena Manager, and Coach journeys pass with correct authorization."

| | |
| --- | --- |
| Required boundary/evidence | All seven roles land on their correct destination, see the correct nav, can reach their one authorized action, and cannot bypass authorization by direct navigation or a real HTTP mutation — Viewer specifically has zero enabled mutation action anywhere. |
| Evidence merged to `main` | **Player, Guardian, Official**: `e2e/role-home-journeys.js` (merged, PR #331) — correct landing (`player_home`/`guardian_home`/`inbox`), not the operator Dashboard. **League Admin, Arena Manager**: `e2e/home-tasks-hub.js` (merged) covers Setup-hub depth for these two roles specifically. **Coach**: `e2e/coach-scope.js` (merged) covers only the *admin-side* account-scoping mechanism (does a Coach account get created with a Team, does the drawer reveal the Team field) — it does **not** test the Coach's own sign-in/landing/nav/authorization journey. **Viewer**: checked every merged e2e file (`grep -rln "viewer" e2e/*.js`) — `context-switcher.js` uses a viewer account only incidentally, to exercise read-only context-switching; no merged file asserts Viewer has zero enabled mutation controls anywhere. |
| Candidate evidence in active PR | #353 (head `c682d3bb1`) adds `e2e/role-authorization-matrix.js`, a from-scratch matrix covering all seven roles with real authenticated sessions, keyboard-reached authorized actions, unauthorized-absent checks, direct-nav bypass probes, and **real negative HTTP mutations expecting 403** for six of the seven roles (verified present: `grep -n "403\|forbidden" role-authorization-matrix.js` at this head returns the described assertions). Explicitly does not duplicate `role-home-journeys.js` or `home-tasks-hub.js`. Full CI green on `c682d3bb1` (9/9), including one pre-existing, unrelated local flake (`context-switcher`) the PR body reproduces on a clean `origin/main` checkout to prove it isn't caused by this branch. |
| Remaining gap | Merge #353. Its own local-suite note (38/39, the one failure pre-existing and reproduced independently of this branch) should be re-verified once landed, since a merge can change timing/ordering even for an unrelated flake. |
| **Status** | **`Pending active PR`** (#353 @ `c682d3bb1`) for Coach's own journey and Viewer's authorization matrix; **`Verified on main`** for Player/Guardian/Official landings and League Admin/Arena Manager Setup-hub depth |

### Criterion 7 — "Desktop, 390px, breakpoint-boundary, keyboard, screen-reader, WCAG 2.2 AA, and zero-console-error evidence is attached."

This criterion bundles six distinct evidence types; each is tracked separately below since they are at very different stages.

| Sub-item | Evidence merged to `main` | Candidate evidence in active PR | Status |
| --- | --- | --- | --- |
| Desktop + 390×844 | Standard convention across every merged e2e journey (`e2e/*.js`, viewport pairs, e.g. `1440×900`/`390×844`). | n/a — already the baseline convention every PR follows. | `Verified on main` (as a baseline convention; not a completion claim for the whole redesign, which is still in progress) |
| Breakpoint-boundary (the four approved tokens 480/720/880/1040) | `styles.css`'s two out-of-contract widths (Game Sheet 680px, Ice Builder 460px) fixed to 720/480 (PR #351, merged `49d662a`), with `e2e/breakpoint-contract.js` (static guard, scoped to `styles.css` only) and `e2e/breakpoint-boundaries.js` (computed-layout Playwright evidence) both registered in CI. **`onboarding.css` (760px) and `setup.css` (520px) remain out-of-contract on `main` today** — PR #351's own body flagged these as an out-of-scope finding for follow-up, matching `operator-ux-requirements.md` §6's own pre-identified list of values needing consolidation. | #354 (head `b5e89612e`) retargets all four production stylesheets (adds `onboarding.css`/`setup.css` fixes) and widens the guard (`check-breakpoint-contract.js`) to read every `<link rel="stylesheet">` in `index.html`/`setup.html`, not just `styles.css`. **Stale-claim finding**: PR #354's body's Test Plan checkmarks (`[x] npm run check-breakpoint-contract... zero violations`, `[x] npm run breakpoint-boundaries... all pass`) describe only *local* runs — `gh api repos/.../commits/b5e89612.../check-runs` returns **zero check-runs** for this exact head (no CI has ever run on it), and `gh pr view 354` reports `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY` against current `main`. The PR's checkmarks are not exact-head CI evidence and the branch cannot currently merge cleanly — call this out rather than treat the checkmarks as authoritative. | `Verified on main` for `styles.css` only; `Missing`/blocked for `onboarding.css` and `setup.css` (PR #354 exists but is unverified-by-CI and merge-conflicting as of this audit) |
| Keyboard-only (manual pass) | `docs/product/manual-keyboard-screenreader-validation-protocol.md` (merged, PR #350, `e8c7d96`) — a **protocol and blank evidence template only**; the document's own Status section states no manual pass has been run under it. | n/a | `Human-only / unperformed` |
| Screen-reader (manual pass) | Same protocol document as above; same caveat. | n/a | `Human-only / unperformed` |
| WCAG 2.2 AA (automated) | `axe-core@^4.12.1` is a declared `devDependency` (`e2e/package.json:48`) but **`grep -n "axe" e2e/accessibility-foundations.js` returns zero matches** — no merged journey actually invokes it. `accessibility-foundations.js` (merged) covers skip-link, per-view titles, and dialog focus/containment (real keyboard behavior), which is a *subset* of WCAG 2.2 AA, not a full automated scan. | #352 (head `f2a0e554d`) adds `e2e/shell-accessibility-coverage.js`, which loads `axe-core` via `require.resolve("axe-core/axe.min.js")` and calls `axe.run(root, {resultTypes: ["violations"]})` (both verified present at this head) across five shell surfaces (login, public, public→staff-sign-in, forced-error, restricted), reporting zero serious/critical violations, plus two production accessibility fixes (color-contrast, status-announcement/focus gaps) found during review. Full CI green on `f2a0e554d` (9/9). | `Pending active PR` (#352 @ `f2a0e554d`) for the five shell surfaces it covers; still `Missing` for the six Setup workflow landings and the Home/Tasks hub itself, which no PR runs axe against yet |
| Zero-console-error | Every merged e2e journey already asserts this as standard convention (`page.on("pageerror"...)`/`page.on("console"...)`, e.g. `accessibility-foundations.js`). | Same convention continued in all four active PRs. | `Verified on main` as a baseline convention across everything merged so far; not yet a claim about the full redesign, most of which isn't merged |

### Criterion 8 — "All three moderated operator-validation sessions are completed and documented."

| | |
| --- | --- |
| Required boundary/evidence | Three real moderated sessions (League Admin, Arena Manager, Coach) — commissioned, run, and documented with completion, timing, interventions, ease rating, and confusion quotes. Not waived, not simulated. |
| Evidence merged to `main` | `docs/product/moderated-operator-validation-protocol.md` (merged, PR #349, `9d090fe`) — a protocol and blank evidence-template document. Its own Status section states explicitly: *"No moderated session has been run under this document."* |
| Candidate evidence in active PR | None — no PR (merged or active) contains a filled-in copy of the evidence template from that protocol. |
| Remaining gap | The three sessions themselves. This is **human-only** work that no code change or automated check can satisfy. |
| **Status** | **`Human-only / unperformed`** — explicitly, per this issue's own repeated language ("not waived or simulated") and the protocol document's own disclaimer. Do not read the merged protocol as progress toward this box. |

### Criterion 9 — "Memory, SQLite, PostgreSQL, authenticated HTTP where relevant, and all required browser CI are green."

| | |
| --- | --- |
| Required boundary/evidence | The **final** #345 implementation PR's exact head is green across the full backend matrix (Memory/SQLite/PostgreSQL) and all required browser CI. |
| Evidence merged to `main` | `main` at `49d662a` itself is green (verified via `gh run view` on the post-merge push-triggered run, not the PR-head result — success). |
| Candidate evidence in active PR | Each of #347, #352, #353 is independently green on its own exact head (9/9 checks each, confirmed live). #354 has **zero CI runs** on its current head and is merge-conflicting (see criterion 7). |
| Remaining gap | This criterion is about the *eventual, complete* #345 PR, which doesn't exist yet — #345 is still split across four independent batches plus the two protocol-only PRs already merged, plus the unaddressed seven-area IA/context-filtering work (criteria 3–4). Green CI on today's individual batches is necessary but not sufficient evidence for this box. |
| **Status** | **`Pending active PR`** for the batches that exist; **`Missing`** as a whole, since no single head yet represents "the complete #345 diff" this criterion actually asks about |

---

## 2. Required state-matrix inventory

Per `operator-ux-requirements.md` §5. `N/A` is used only where a concrete,
cited reason makes the state genuinely inapplicable to that screen — never
to paper over an unimplemented state.

### Home/Tasks hub (`app.js:563-712`, `renderSetupProgressCard`/`loadSetupProgressCard`)

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | `renderSetupProgressCard(null, false, true)` (`app.js:578-582`) | `e2e/home-tasks-hub.js` | Desktop + 390×844, backend (Memory/SQLite/PostgreSQL via `test_setup_progress.py`) | None identified |
| Empty | `!progress \|\| !progress.program_id` returns `""` (`app.js:601`) | Not explicitly asserted as a *named* empty state in `home-tasks-hub.js` (renders literally nothing) | Not demonstrated as a distinct, message-bearing state | This branch renders a **blank card**, not a named empty-state message — worth a explicit look: is "no active Program yet" reachable in practice, or structurally excluded? Not resolved by this audit; flagged rather than assumed benign |
| Stale | `setupProgressFetchSeq` monotonic guard (`app.js:161,721,729`) | `e2e/home-tasks-hub.js` (held-response races, per PR #331's own test plan) | Desktop + 390×844 | None identified |
| Per-card error + retry | `hadError` branch (`app.js:584-599`), `data-setup-progress-retry` | `e2e/home-tasks-hub.js` | Desktop + 390×844 | None identified |
| Confirmation | n/a | n/a | n/a | **N/A** — this card contains no destructive action; confirmation lives in the drill-in workflows it links to |
| Success/complete | `progress.complete` branch (`app.js:602-640`), including per-workflow `attention` rows | `e2e/home-tasks-hub.js` | Desktop + 390×844 | None identified |
| Optional (Workflow 6) | Distinct "Optional" badge, never `next` (`app.js:643-649`) | `e2e/home-tasks-hub.js` | Desktop + 390×844, backend (`get_setup_progress` optional-status contract) | None identified |

### League profile and seasons

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Shared whole-pane `render()` skeleton (`app.js:6098`) — no per-card skeleton for this workflow specifically | None specific to this workflow | Implicit only, via the shared skeleton | Per-card skeleton for the new landing (#347's `renderSetupWorkflowLanding`, pending, does not add one — see criterion 5) |
| Empty | Pre-existing `.setup-card`'s `.empty` div (`app.js` `setupCard()`) for the *old* mega-page's League/Season cards | `e2e/season-rollover.js`, `e2e/hierarchy-import.js` (indirect) | Desktop + 390×844 for the old card shape | The required §5 recipe ("No seasons yet" + "Add Season" link, named count/scope) is not confirmed against this exact copy; the *new* landing (#347) shows only a raw count via `setupSummaryHtml()`, not a message |
| Stale | Shared whole-pane `render()` refetch on nav/context change | Not specifically asserted for this workflow | Not demonstrated | Per-card stale-guard, same gap as loading |
| Per-card error + retry | Shared whole-pane `#retry-btn` (`app.js:6425`) — **whole-pane, not per-card** | Not specific to this workflow | Whole-pane only | Per-card boundary required by §5 for hub-shaped screens; not delivered anywhere yet |
| Confirmation | Archiving/deleting a Season — pre-existing named-resource modal | `e2e/season-rollover.js`, `e2e/season-dates.js` | Desktop + 390×844 | Not yet re-verified against the *new* landing's entry point (only against the old mega-page path) |
| Success/complete | `get_setup_progress`'s `done` status for this workflow key | `e2e/home-tasks-hub.js` (via the progress card, not this workflow's own landing) | Desktop + 390×844 | This workflow's *own* landing has no independent complete-state messaging (e.g., "Season configured") beyond the shared progress card |
| Optional | n/a | n/a | n/a | **N/A** — only Workflow 6 carries optional semantics |

### Permanent teams

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Shared whole-pane skeleton only | None specific | Implicit only | Same per-card gap as above |
| Empty | Pre-existing `.setup-card` `.empty` div | `e2e/permanent-teams.js` | Desktop + 390×844 | New landing shows only a count, not the required named message, per #347 |
| Stale | Shared whole-pane refetch | Not specific | Not demonstrated | Same gap |
| Per-card error + retry | Whole-pane only | Not specific | Whole-pane only | Same gap as League profile/seasons |
| Confirmation | Delete/deactivate a team — pre-existing named-resource modal | `e2e/records-delete.js`, `e2e/safe-destructive.js` | Desktop + 390×844 | Not re-verified against the new landing's entry point |
| Success/complete | `get_setup_progress`'s `done` status | `e2e/home-tasks-hub.js` (via progress card) | Desktop + 390×844 | No independent complete-state on this workflow's own landing |
| Optional | n/a | n/a | n/a | **N/A** |

### Season participation/divisions

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Shared whole-pane skeleton only | None specific | Implicit only | Same per-card gap |
| Empty | The existing #311 empty-state recipe, already built for this exact sub-view (per `operator-ux-requirements.md` §5, cited as "reused verbatim") | Referenced in `operator-ux-requirements.md` as issue #311's worked example; underlying journey not independently re-confirmed by this audit's grep pass | Desktop + 390×844 (per prior documentation) | This is the **one workflow** whose empty state already meets the full required recipe — re-verify it is preserved once #347's landing sits in front of it |
| Stale | Existing stale-guard (per §5, "existing") | Not independently re-confirmed here | Not demonstrated by this audit directly | Re-verify against the new landing entry point |
| Per-card/whole-pane error + retry | §5 designates this one **whole-pane** (not per-card), matching a single-purpose detail screen | `e2e/season-participation.js`, `e2e/registration-cleanup.js` | Desktop + 390×844 | None identified beyond re-verifying against the new landing |
| Confirmation | Unregistering a team — pre-existing named-resource modal | `e2e/registration-cleanup.js` | Desktop + 390×844 | Not re-verified against the new landing |
| Success/complete | `get_setup_progress`'s `done`/`attention` status (this is the workflow whose `attention` field the merged progress card explicitly surfaces, per `app.js:650-661`) | `e2e/home-tasks-hub.js` | Desktop + 390×844 | No independent complete-state on the workflow's own landing |
| Optional | n/a | n/a | n/a | **N/A** |

### Clubs, players and staff

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Shared whole-pane skeleton only | None specific | Implicit only | Same per-card gap |
| Empty | Pre-existing `.setup-card` `.empty` div | `e2e/player-lifecycle.js`, `e2e/player-edit.js` | Desktop + 390×844 | New landing shows only a count (via `setupSummaryHtml`), not the required "No players yet" + link message |
| Stale | Shared whole-pane refetch | Not specific | Not demonstrated | Same gap |
| Per-card error + retry | Whole-pane only | Not specific | Whole-pane only | Same gap |
| Confirmation | Deactivate/delete a player — pre-existing named-resource modal | `e2e/player-lifecycle.js` | Desktop + 390×844 | Not re-verified against the new landing |
| Success/complete | `get_setup_progress`'s `done` status | `e2e/home-tasks-hub.js` | Desktop + 390×844 | No independent complete-state on this workflow's own landing |
| Optional | n/a | n/a | n/a | **N/A** |

### Venues, rinks and ice

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Shared whole-pane skeleton (landing); Ice Availability Builder's own preview/commit skeleton (drill-in) | `e2e/ice-availability-builder.js` | Desktop + 390×844 | Landing-level per-card skeleton still missing, same as other workflows |
| Empty | Landing: pre-existing `.setup-card` `.empty`; drill-in ("no ice on this rink yet") per §5's target | `e2e/allowed-venues.js`, `e2e/venue-sharing.js`, `e2e/ice-availability-builder.js` | Desktop + 390×844 | New landing shows only counts, not the required named messages |
| Stale | Existing stale-guard on the builder's preview-vs-commit cycle (§5, "existing, keep") | `e2e/ice-availability-builder.js` | Desktop + 390×844 | Landing-level stale-guard still missing |
| Error + retry | §5 designates **per-card on the landing summary**, **whole-pane on a single venue/rink drill-in** | Whole-pane drill-in behavior exists (pre-#345); per-card landing behavior does not | Whole-pane only, on the drill-in | Per-card landing boundary not delivered anywhere |
| Confirmation | Deleting a venue/rink/ice slot — pre-existing named-resource modal (`destructive-surfaces` pattern) | `e2e/destructive-surfaces.js`, `e2e/venue-access-cleanup.js` | Desktop + 390×844 | Not re-verified against the new landing's entry point |
| Success/complete | `get_setup_progress`'s `done` status | `e2e/home-tasks-hub.js` | Desktop + 390×844 | No independent complete-state on this workflow's own landing |
| Optional | n/a | n/a | n/a | **N/A** |

### Imports and onboarding (Workflow 6)

| State | Production entry point/symbol | Existing automated journey/assertion | Coverage demonstrated | Missing behavior/evidence |
| --- | --- | --- | --- | --- |
| Loading | Skeleton during file parse/validation (§5 target); onboarding wizard's existing loading behavior | `e2e/onboarding-wizard.js`, `e2e/hierarchy-import.js` | Desktop + 390×844 | Not independently re-verified for the *unified* "Import data" entry point #347 introduces (today's onboarding wizard and the standalone Import tab are still two separate flows on `main`, per `operator-ux-requirements.md` §4's own "not yet unified" note) |
| Empty | "No import in progress — upload a file to begin" (§5 target) | Not confirmed present verbatim in any merged or pending file by this audit's grep pass | Not demonstrated | Confirm this exact copy exists, or flag as a genuine gap in a follow-up |
| Stale | Existing dry-run-vs-commit pattern (§5, "existing, keep") | `e2e/hierarchy-import.js` | Desktop + 390×844 | None identified beyond the unification question above |
| Error + retry | Whole-pane (§5: "a wizard, single-purpose per step") | `e2e/onboarding-wizard.js`, `e2e/hierarchy-import.js` | Desktop + 390×844 | None identified |
| Confirmation | Committing an import with warnings — named-resource-style refusal naming affected rows (§5 target, "existing `ice_slot_overlap`-style copy, keep") | `e2e/hierarchy-import.js` | Desktop + 390×844 | None identified |
| Success/complete | `get_setup_progress`'s handling of Workflow 6 once workflows 1–5 are done (must never read as blocking) | `e2e/home-tasks-hub.js` | Desktop + 390×844 | None identified |
| Optional | Distinct "Optional" badge (`app.js:643-649` on the progress card; `optional: true` in #347's `SETUP_WORKFLOWS`, pending) | `e2e/home-tasks-hub.js` (merged); `e2e/setup-workflow-hub.js` (pending, #347) | Desktop + 390×844 for the merged half | The *landing's own* optional badge/copy is pending #347, not yet on `main` |

---

## Stale or contradictory claims found (called out, not silently reconciled)

1. **PR #354's Test Plan checkmarks are local-only, not exact-head CI evidence, and its branch cannot currently merge cleanly.** The PR body checks off `npm run check-breakpoint-contract` and `npm run breakpoint-boundaries` as passing, but `gh api .../commits/b5e89612.../check-runs` returns zero check-runs for that exact head, and `gh pr view 354` reports `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY` against current `main`. Treat this PR's evidence as unverified by CI and currently blocked, not as ready-to-merge "pending" evidence on the same footing as #347/#352/#353.
2. **PR #347 is based on an older `main` commit (`4279ca4`, before PR #350 and #351 merged).** GitHub reports it as `MERGEABLE`/`CLEAN` regardless, but it has not been re-run against the current `main` tip — its green CI reflects the tree at its own base plus its own commits, not the exact tree that would result from merging it today.
3. **`axe-core` has been a declared dependency in `e2e/package.json` since before this audit, but no merged journey calls it.** A reader could reasonably assume the dependency's presence means automated WCAG scanning already runs somewhere on `main` — it does not; the only caller is PR #352's new, unmerged `shell-accessibility-coverage.js`.
4. **`e2e/coach-scope.js` is a real, merged, passing journey, but it does not test what its name might suggest to a reviewer scanning file names for "Coach coverage."** It tests the *admin-side* account-scoping mechanism (does creating a Coach account correctly require/attach a Team), not the Coach role's own sign-in/landing/authorization journey — that gap is what PR #353 fills.
5. **`ROADMAP.md`'s "Currently active sequencing" section still describes #345 as one undifferentiated deliverable** ("guided Setup, seven-area IA, accessibility, and operator validation completion (#345)") and does not yet reflect the batch split now visible across #347/#349/#350/#351/#352/#353/#354. This is not necessarily wrong (the batches are all still "part of #345"), but a reader relying on `ROADMAP.md` alone would not learn that #345 has already been split into seven-plus tracked pieces, three of them merged. Flagged for the owner's awareness; not fixed here per this task's scope boundary (`ROADMAP.md` is explicitly out of scope for this PR).

---

## Falsifiability self-checks (performed before opening this PR)

Per this task's own requirement, both checks below were performed by hand
against the drafted document, then reverted — recorded here, not as product
evidence.

1. **Removed-citation check.** In Criterion 1's "Evidence merged to `main`"
   cell, the citation to `e2e/home-tasks-hub.js` (merged via PR #331,
   `16fe833`) was temporarily deleted, leaving only the `app.js` symbol
   references. Re-reading the row with that gap: the status `Verified on
   main` becomes unsupportable — a symbol existing in source is not, by
   itself, evidence it's exercised by a passing test, so the row would have
   to drop to at best `Missing` evidence-for-status (even though the code
   is real). This confirmed the ledger's own status rule ("merged
   **and** the cited evidence covers the entire criterion") actually bites
   when a citation is missing, rather than accepting a bare symbol
   reference as sufficient. The citation was restored.
2. **Mislabeled-status check.** Criterion 7's breakpoint-boundary row was
   temporarily edited to mark the `onboarding.css`/`setup.css` half as
   `Verified on main` (instead of `Missing`/blocked), citing PR #354 as if
   it were merged. Re-reading the row against the ledger's own status
   definitions immediately surfaces the contradiction: `Verified on main`
   requires the implementation to be *merged*, and #354 is an open,
   `CONFLICTING` draft PR with zero CI runs on its head — the mislabel is
   inconsistent with the very SHA/mergeability facts recorded two columns
   over in the same row. This confirmed the four-way status vocabulary is
   restrictive enough to catch a status/evidence mismatch on a re-read, not
   just at write time. The correct `Missing`/blocked label (with the #354
   citation kept as pending, non-authoritative candidate evidence) was
   restored.
