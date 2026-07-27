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

## Freshness snapshot

**This ledger is a snapshot, not a live view.** It reflects the current
state of `main` at the time this document is read. The one remaining active
PR it cites (#354) is still a moving branch — its head, blockers, and CI
results can change. **Snapshot semantics:** This document is accurate for
the stated `main` SHA and UTC timestamp. **#345's issue checklist and
current GitHub state remain authoritative for later merge readiness** — if
#354 advances, new active PRs are opened, or #345's acceptance boxes
change, this document's status values become stale until a fresh refresh.

- **Snapshot taken**: `2026-07-27T13:45:00Z` (all SHAs, mergeable states,
  and CI results below were re-checked live at this time, not carried over
  from an earlier read).
- **Base branch**: `main` at `9447bb69f69209c86f44377da212ff9f9f2fd716` (merge
  of PR #356, League-context backend foundation).

## Revision history

**Round 1 corrections** (per the review at
[2026-07-27T10:38Z](https://github.com/jingizoo/biknik/pull/355#issuecomment-5090454332)),
re-read from the same base plus all current comments on #347/#352/#353 at
that time:

1. Added each PR's live, unresolved review blocker beside its pending
   citation — a green exact-head CI run is necessary evidence, not
   resolution of a blocker.
2. Corrected criterion 4, which previously said #347 provides "None"/
   "entirely unaddressed" context evidence — it provides real partial
   evidence, just not the full criterion.
3. Collapsed criteria 5, 6, 7, and 9 to exactly one overall status each.
4. Narrowed the zero-console-error claim (criterion 7) to a verified
   inventory of the 35 real browser journeys.

**Round 2 corrections** (per the freshness review at
[2026-07-27T11:09Z](https://github.com/jingizoo/biknik/pull/355#issuecomment-5090595587)):
Round 1's content was already stale by the time it was reviewed — two of
the three cited PRs had advanced. This revision re-read every current
comment and the live head/base/mergeable-state/CI for all four PRs as of
the snapshot timestamp `2026-07-27T11:14:44Z`:

1. **#347 advanced to `746c823e4e5733c04cf6a4f390cc2c54398dc66a`**, now
   based on current `main` (`49d662a`, no longer behind). The Division
   multi-Season write blocker Round 1 described as open is **now fixed**
   per the exact-head reviewer's own confirmation — replaced with the two
   *new* blockers that head actually carries (positional `actor_id`
   compatibility break; the required browser proof is still mouse-driven,
   not keyboard).
2. **#353 advanced to `057e9e8398fdc0be74a2044eb369c280c3807584`.** The
   developer reports the audit-boundary blocker fixed with real
   falsifiability evidence, but this is a **developer-reported correction
   awaiting reviewer verification and CI completion** (postgres was still
   `pending` at snapshot time) — not yet marked resolved here, per the
   freshness rule's explicit instruction not to infer readiness from a new
   push.
3. Removed the "stale claims" entry stating #347 was based on an older
   `main` commit — that statement is itself now false, since #347's base
   advanced to current `main` in the same push described in (1).
4. Refreshed the summary table, criteria 2/4/6/9, and the PR body to match.

**Round 3 refresh** (this revision, `2026-07-27T13:45:00Z`): Four PRs have
merged to current `main` since the Round 2 snapshot. This refresh moves their
evidence from pending to verified and updates affected criteria. Four new
tracking issues (#357–#360) are now recorded as planned/open work, never as
completion evidence:

1. **#347** (Guided Setup hub) merged via commit `5dfa6e0` on
   2026-07-27T11:59:39Z. The two prior blockers (positional-argument
   compatibility, keyboard browser proof) were resolved before merge — CI
   was green and the PR was converted from draft to ready.
2. **#352** (Shell accessibility) merged via commit `91c5e2a` on
   2026-07-27T11:26:59Z. The prior blocker (non-falsifiable stale-response
   regression) was resolved — the held route now resolves to a distinct
   stale fixture, the guard is verified falsifiable by temporarily
   disabling it and confirming test failure, and axe-core scans report zero
   serious/critical violations.
3. **#353** (Seven-role authorization matrix) merged via commit `322a959`
   on 2026-07-27T12:20:26Z. The audit-boundary blocker was resolved and
   verified by the reviewer — the test now snapshots the audit boundary
   and is falsifiable.
4. **#356** (League-context backend) merged via commit `80368b3` on
   2026-07-27T13:05:50Z. All backend/service work for persistent League
   context is complete, tested (25 new tests, all three backend types
   green), and merged to `main`.
5. Rebased PR #355 to current `main` (`9447bb6`), re-read every affected
   criterion and the merged PR descriptions to confirm each cited evidence
   is accurate.
6. #354 (Breakpoint consolidation) remains open/draft, still `CONFLICTING`
   against current `main`, with zero CI runs.

## Base and inspected heads

- **Base**: `main` at `9447bb69f69209c86f44377da212ff9f9f2fd716` (merge of
  PR #356, League-context backend foundation).
- **Merged PRs** (verified via GitHub merge metadata and exact head SHA):

  | PR | Title | Merge commit | Merged at | Evidence for which criteria |
  | --- | --- | --- | --- | --- |
  | [#347](https://github.com/jingizoo/biknik/pull/347) | Guided Setup hub: six summary-first workflow landings | `5dfa6e0b5711ddd5fbb80e7ebdd3ff148aafaea7` | 2026-07-27T11:59:39Z | Criterion 2 (hub structure + optional contract), Criterion 4 (context seeding in secondary actions) |
  | [#352](https://github.com/jingizoo/biknik/pull/352) | Cover login, public, error, and restricted shell states | `91c5e2a79d4cfa385d957c6205acb8f1431990d3` | 2026-07-27T11:26:59Z | Criterion 7 (WCAG 2.2 AA via axe-core, with three production accessibility fixes; stale-response regression is now falsifiable) |
  | [#353](https://github.com/jingizoo/biknik/pull/353) | Add seven-role destination and authorization matrix | `322a9594b103994b647ad6038b802a40f380f61d` | 2026-07-27T12:20:26Z | Criterion 6 (all seven roles with keyboard navigation, authorization probes, and audit-boundary snapshots; falsifiability verified by reviewer) |
  | [#356](https://github.com/jingizoo/biknik/pull/356) | Add persistent League-context foundation | `80368b38d1b967d7df38b3fc3c14453ead55e2af` | 2026-07-27T13:05:50Z | Backend foundation for Criterion 4 (League context binding and authorization, 25 new tests, Memory/SQLite/PostgreSQL all green) |

- **Remaining open PR**:

  | PR | Title | Head SHA | Base SHA | Mergeable state | Blocker |
  | --- | --- | --- | --- | --- | --- |
  | [#354](https://github.com/jingizoo/biknik/pull/354) | Consolidate responsive breakpoints to 480/720/880/1040 (all four production stylesheets) | `b5e89612ed297f0df3e97889482a6f369daa63c1` | `49d662a` (old `main`, now behind) | **`CONFLICTING` / `DIRTY`**, zero CI runs on this head | Reintroduces already-merged PR #351's canonical `styles.css`/guard changes as a competing duplicate (authored from pre-#351 tree); required to rebase from current `main`, drop the already-landed changes, and extend #351's canonical breakpoint guard |

- **Other sources read in full**: `ROADMAP.md` (current), issue
  [#345](https://github.com/jingizoo/biknik/issues/345) (body + all 7
  review/status comments), `docs/product/operator-ux-requirements.md`,
  `docs/product/moderated-operator-validation-protocol.md`,
  `docs/product/manual-keyboard-screenreader-validation-protocol.md`, and
  **every current comment** on #347 (8 comments), #352 (5 comments),
  #353 (5 comments), and #354 (1 comment) — not only each PR's body and CI
  checks, since the live review blockers recorded below only surface in
  the comment threads. Re-read in full again for this revision (see
  Revision history above) since #347 and #353 had each advanced since the
  prior read.

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

**Each top-level acceptance criterion in §1 carries exactly one overall
status from the four values above — never more than one, never zero.**
Where a criterion bundles several distinct evidence types (criterion 7) or
where the underlying screens/roles are at different stages (criteria 2, 4,
5, 6, 9), that nuance belongs in the row's supporting text and any
sub-item table, not in a second status value attached to the criterion
itself. The decision rule for picking the one overall value:

- if any required portion of the criterion has no implementation or no
  evidence at all, the criterion is `Missing`, even if other portions are
  merged;
- if the *entire* criterion could be satisfied once a specific active PR's
  open review blocker(s) close — i.e., every required portion already has
  real, cited evidence somewhere (merged or in that one PR) — the criterion
  is `Pending active PR`, and the row must name the blocker(s), not just
  the PR;
- `Verified on main` is never used for a criterion where any required
  portion is a subset, a sub-item, or a single screen/role rather than the
  whole.

---

## 1. Acceptance-criterion ledger

### Criterion 1 — "A first-time League Admin can identify and open the correct next incomplete setup step from one primary action."

| | |
| --- | --- |
| Required boundary/evidence | Home/Tasks landing computes the next incomplete workflow without the operator reading raw entity data, exposes exactly one primary action, and that action opens the correct destination directly (not merely the Setup tab). |
| Evidence merged to `main` | `renderSetupProgressCard()` (`backend/hockey_scheduler/web/static/app.js:563-712`) renders a single `data-setup-progress-action` primary button naming the real next-incomplete workflow in operator vocabulary (`next.label`/`next.primary_action`), backed by `get_setup_progress` (`backend/tests/test_setup_progress.py`). `goToSetupWorkflow(key)` (`app.js:762-828`) deep-links that one click straight into the correct destination for all six keys: a context-seeded, fail-closed create drawer for `league_season`/`teams`/`roster` (`app.js:775-814`, `contextSeededDrawerValues()` at `app.js:867+`), a focused Register-Team control for `participation` (`focusParticipationRegisterControl()`), the Ice Availability Builder for `facilities`, and the Import tab for `import`. Regression: `e2e/home-tasks-hub.js` (merged via PR #331, `16fe833`), asserting the deep-link lands on the real control (not just the tab) for each key, at desktop and 390×844. |
| Candidate evidence in active PR | None needed beyond what's merged — this criterion concerns the *action*, not the six-workflow *hub landing screen* (that's criterion 2). #347 (head `746c823e4`) does not change `goToSetupWorkflow`'s already-merged behavior for the four keys carried over from PR #331; its own new code only affects **secondary/tertiary** actions inside the new landing (see criterion 2). |
| Remaining gap | None identified for the literal criterion text. |
| **Status** | **`Verified on main`** |

### Criterion 2 — "All six workflows are reachable through summary-first hub entries with existing capability preserved and Workflow 6 follows the approved optional contract."

| | |
| --- | --- |
| Required boundary/evidence | A hub index listing all six #204-named workflows, each with its own summary-first landing (not the old undifferentiated Setup mega-page as the *only* route); Workflow 6 ("Imports and onboarding") carries a third status distinct from done/todo, never `next`, never blocking. |
| Evidence merged to `main` | **Yes.** `SETUP_WORKFLOWS` (`app.js:2912`), `renderSetupHub()` (`app.js:2989`), and `renderSetupWorkflowLanding()` (`app.js:3015`) are present on current `main` via PR #347 merge (`5dfa6e0`). Hub index of six cards plus one summary-first landing per workflow, each with exactly one `.act.primary`. The old Records/Hierarchy sub-views remain reachable via the existing segmented toggle, so no previously-reachable screen became unreachable. Workflow 6 carries `optional: true`, a distinct "Optional" badge, and its own explanatory copy, never competing with the five required workflows for the hub's `next` recommendation. The Division multi-Season write blocker is fixed (verified by exact-head reviewer 2026-07-27T11:08:08Z). The two subsequent blockers (positional-argument compatibility, keyboard browser proof) were also resolved before merge — the PR was converted from draft to ready and merged on 2026-07-27T11:59:39Z with 9/9 CI green. |
| Candidate evidence in active PR | None — the entire criterion is now merged. |
| Remaining gap | Per-card loading/empty/error/retry for the new landings (see criterion 5 — this is a distinct, still-open gap not part of criterion 2's own boundary). |
| **Status** | **`Verified on main`** |

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
| Evidence merged to `main` | **Partial — three components, varying coverage:**<br><br>**(1) Context switcher (pre-existing).** The mechanism itself (`#context-switcher` wrapper / `#ctx-select` native `<select>`, `index.html:111-117`) is pre-existing (#159/#322/#323) and unchanged.<br><br>**(2) Context seeding + Division multi-Season fix (merged).** PR #347 (`5dfa6e0`) delivers real, verified Program/Season context evidence for the Setup-hub's secondary/tertiary actions (Leagues/Divisions/Rinks/Add-one-ice-slot): `contextSeededDrawerValues()` seeds those actions from the active Program/Season rather than a global-first fallback, fails closed on missing/stale/mismatched context, and `e2e/setup-workflow-hub.js` verifies the persisted record's Program reads back correctly from the server. The multi-Season Division write is now fixed (verified by reviewer 2026-07-27T11:08:08Z), and PR #347 merged with 9/9 CI green.<br><br>**(3) League backend foundation (merged).** PR #356 (`80368b3`) adds backend support for persistent League context: `ActiveContext.league_id`, `exact_league_season_or_conflict()`, `authorized_league_ids()`, `resolve_with_league` / `options_with_league` / `set_with_league` methods. 25 new tests, all three backend types green (Memory/SQLite/PostgreSQL). This is the **additive backend foundation** only — HTTP integration and context-bar UI promotion are separate following slices, not yet merged. |
| Remaining gap | League promotion into the persistent **UI** context bar — still no implementation touching `index.html` navigation/context display. General changed-screen filtering or removal/narrowing of the `ctx-unfiltered` caption (`index.html:117`). These two together still represent a required portion with no merged evidence. |
| **Status** | **`Missing`** — a required portion (League UI promotion, general screen filtering) has no implementation or evidence on `main`, which is decisive for the overall criterion even though #347 and #356 together provide genuine partial evidence for the narrower context-seeding and backend-foundation pieces |

### Criterion 5 — "Loading, empty, stale, error, retry, confirmation, optional, and complete states match the approved matrix."

| | |
| --- | --- |
| Required boundary/evidence | Per `operator-ux-requirements.md` §5, the full states matrix applies to Home/Tasks and each of the six Setup workflows (per-card error boundaries for hub-shaped screens, named empty states with the #311 recipe, stale-response guards, named-resource confirmation modals, and Workflow 6's distinct optional status). |
| Evidence merged to `main` | Home/Tasks hub has substantial, but not complete, state coverage: skeleton loading (`renderSetupProgressCard(_, _, true)`, `app.js:578-582`), per-card error + retry (`hadError` branch, `app.js:584-599`, `data-setup-progress-retry`), stale-response guard (`setupProgressFetchSeq`, `app.js:161,721,729`), success/complete (`progress.complete` branch, `app.js:602-640`), and a distinct optional badge for Workflow 6 (`app.js:643-649`) are all real and tested. **Corrected from an earlier draft**: Home/Tasks hub's own Empty state (see the §2 inventory) is an unverified blank-render branch (`!progress \|\| !progress.program_id` returns `""`, `app.js:601`), not a confirmed, message-bearing empty state — so even Home/Tasks alone does not close the full required set of states. The six Setup workflows themselves route through the single, pre-#345 `renderSetup()`/whole-pane `render()` skeleton (`app.js:6098`) and whole-pane `#retry-btn` (`app.js:6425`) — **not** per-card — for every one of the seven required states. |
| Candidate evidence in active PR | None — #347 (merged `5dfa6e0`) added the landing *structure* (summary counts via `setupSummaryHtml()`) but, by its own PR body ("Explicitly NOT in this batch: ... the full state matrix"), does not add per-card loading/error/retry/stale-guard to the new landings — they inherit the same whole-pane behavior as the pre-#345 mega-page. Its landing's own "empty" case is a bare `<div class="empty">Your role doesn't manage any setup workflows.…</div>` for a *role* with zero permitted workflows, not the required per-workflow "No seasons yet" / "No teams yet" recipe. Tracked under #358 (seven-area IA). |
| Remaining gap | Per-card loading/empty/stale/error/retry for all six new workflow landings; a confirmed, message-bearing empty state for Home/Tasks hub itself. The confirmation-modal convention already exists for entity deletes in pre-existing drill-in views but is not yet re-verified against the *new* landing entry points. These are tracked in #358 and #359 (broader axe coverage). |
| **Status** | **`Missing`** — the six Setup workflows' landing-level states have no per-card implementation anywhere, and even Home/Tasks hub's own Empty state is unverified, so no required portion of this criterion is a closed set |

### Criterion 6 — "Player, Guardian, Official, Viewer, League Admin, Arena Manager, and Coach journeys pass with correct authorization."

| | |
| --- | --- |
| Required boundary/evidence | All seven roles land on their correct destination, see the correct nav, can reach their one authorized action, and cannot bypass authorization by direct navigation or a real HTTP mutation — Viewer specifically has zero enabled mutation action anywhere. |
| Evidence merged to `main` | **Yes.** PR #353 (`322a959`) merged on 2026-07-27T12:20:26Z with all seven roles covered. `e2e/role-authorization-matrix.js` provides a from-scratch matrix covering all seven roles with real authenticated sessions, bounded keyboard `Tab`/`Shift+Tab` traversal, unauthorized-absent checks, direct-nav bypass probes, and real negative HTTP mutations with a precise per-response failure tracker. The audit-boundary blocker was resolved before merge: `assertForbiddenNoChange()` now snapshots both the setup audit (via `/api/demo/overview`) and per-game audit arrays (via `/api/games/{id}/board`), and was verified falsifiable by the reviewer. All 9/9 CI checks were green on merge. Merged PR description confirms all seven roles, their landings, nav visibility, keyboard navigation to authorized actions, and authorization rejection evidence. |
| Candidate evidence in active PR | None — the entire criterion is now merged. |
| Remaining gap | None identified. |
| **Status** | **`Verified on main`** |

### Criterion 7 — "Desktop, 390px, breakpoint-boundary, keyboard, screen-reader, WCAG 2.2 AA, and zero-console-error evidence is attached."

This criterion bundles six distinct evidence types at very different
stages. Per-sub-item detail is kept in the table below for traceability,
but — per the status-vocabulary rule above — **the criterion as a whole
carries exactly one overall status, given after the table**, not one per
row.

| Sub-item | Evidence merged to `main` | Candidate evidence in active PR | Sub-item stage |
| --- | --- | --- | --- |
| Desktop + 390×844 | Standard convention across every merged e2e journey (`e2e/*.js`, viewport pairs, e.g. `1440×900`/`390×844`). | n/a — already the baseline convention every PR follows. | Verified as a baseline convention; not a completion claim for the whole redesign |
| Breakpoint-boundary (the four approved tokens 480/720/880/1040) | `styles.css`'s two out-of-contract widths (Game Sheet 680px, Ice Builder 460px) fixed to 720/480 (PR #351, merged `49d662a`), with `e2e/breakpoint-contract.js` (static guard, scoped to `styles.css` only) and `e2e/breakpoint-boundaries.js` (computed-layout Playwright evidence) both registered in CI. **`onboarding.css` (760px) and `setup.css` (520px) remain out-of-contract on `main` today** — PR #351's own body flagged these as an out-of-scope finding for follow-up. | #354 (head `b5e89612e`) retargets all four production stylesheets and widens the guard. **Stale-claim finding**: PR #354's Test Plan checkmarks describe only *local* runs — its exact head has **zero CI check-runs** and `gh pr view 354` reports `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY` against current `main`. | `styles.css` verified merged; `onboarding.css`/`setup.css` blocked — #354 exists but is unverified-by-CI and merge-conflicting |
| Keyboard-only (manual pass) | `docs/product/manual-keyboard-screenreader-validation-protocol.md` (merged, PR #350, `e8c7d96`) — a **protocol and blank evidence template only**; the document's own Status section states no manual pass has been run under it. | n/a | Human-only, unperformed |
| Screen-reader (manual pass) | Same protocol document as above; same caveat. | n/a | Human-only, unperformed |
| WCAG 2.2 AA (automated) | **Yes.** `axe-core@^4.12.1` is a declared `devDependency` (`e2e/package.json:48`), and is now actively used. `e2e/shell-accessibility-coverage.js` (merged via PR #352, `91c5e2a`) loads `axe-core` and calls `axe.run(root, {resultTypes: ["violations"]})` across five shell surfaces (signed-out login, public schedule, staff sign-in transition, forced loading/error, restricted early-return), reporting zero serious/critical violations on all surfaces. Three production accessibility fixes were found and merged with the PR: color-contrast overrides for login/public screens, ARIA role + live-region semantics on error/status content, and focus management on persistent controls. The prior falsifiability blocker was resolved: the held route now resolves to a deliberately distinct stale fixture, the guard is verified load-bearing by temporarily disabling it and confirming test failure, and all 9/9 CI checks were green on merge (2026-07-27T11:26:59Z). `accessibility-foundations.js` (merged, pre-#345) covers skip-link, per-view titles, and dialog focus/containment as a *subset* of WCAG 2.2 AA. | Five shell surfaces verified merged; six Setup workflow landings and Home/Tasks hub itself still have no axe scanning (pending broader accessibility work) |
| Zero-console-error | **Verified inventory, not an assumed convention.** Of the 35 files under `e2e/*.js` that are real Playwright browser journeys (`require("playwright")` present), **34 install both `page.on("pageerror", ...)` and `page.on("console", ...)` tracking** (confirmed by `grep -c` across every file, e.g. `accessibility-foundations.js`, `home-tasks-hub.js`, `role-home-journeys.js`). The one exception, `api-error-resilience.js`, installs `pageerror` tracking only, by design — it deliberately provokes 401/403/502 responses and does not assert a zero-console-error bar for itself. The six other `e2e/*.js` files (`breakpoint-contract.js`, `check-v1-route-contract.js`, `ci-classify.js`/`.test.js`/`.integration.test.js`, `season-fmt-unit.js`) are static/unit checks, not browser journeys, and are correctly outside this claim's scope. | Same tracking convention continued in the merged journeys (#347, #352, #353). | Verified for 34/35 merged browser journeys as a baseline convention; not yet a completion claim for the whole redesign, most of which isn't merged |

**Overall status for Criterion 7: `Missing`.** Two of its six required
evidence types (keyboard-only and screen-reader manual passes) have zero
human evidence and cannot be produced by merging any PR — that alone means
a required portion of this criterion has no evidence at all, which is
decisive per the status rule above regardless of how much of the rest is
merged or pending. The WCAG 2.2 AA (automated) sub-item is now merged via
#352 (`91c5e2a`), but the scope is limited to five shell surfaces only —
the six Setup workflow landings and Home/Tasks hub itself lack axe scanning,
tracked under #359 (broader automated axe coverage). The breakpoint-boundary
sub-item (#354) remains unmerged and conflicting, still duplicating
already-merged #351 work. Manual keyboard and screen-reader evidence are
unperformed and tracked under #359.

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
| Evidence merged to `main` | **Yes, for all merged batches.** `main` at `9447bb6` is green across the full matrix (verified post-merge). Individual merged PRs: <br>• #347 (`5dfa6e0`): 9/9 CI green on merge (2026-07-27T11:59:39Z)<br>• #352 (`91c5e2a`): 9/9 CI green on merge (2026-07-27T11:26:59Z)<br>• #353 (`322a959`): 9/9 CI green on merge (2026-07-27T12:20:26Z)<br>• #356 (`80368b3`): all backend types (Memory/SQLite/PostgreSQL) green on merge (2026-07-27T13:05:50Z), browser CI skipped per correct scope boundary (backend-only) |
| Remaining gap | This criterion asks about the *eventual, complete* #345 PR as a whole, which does not yet exist — #345 is still split across the four merged batches plus #354 (still open/conflicting, zero CI runs) plus the unaddressed seven-area IA/context-filtering work (criteria 3–4) plus manual moderated sessions (criterion 8). The four merged batches are each CI-green, but they do not yet constitute "the complete #345 diff." |
| **Status** | **`Missing`** — no single head represents the complete #345 implementation, and unmerged work (criterion 3 seven-area IA, criterion 4 League UI promotion, criterion 5 per-card state matrix, criterion 8 moderated sessions, #354 still unmerged) remains outstanding |

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

1. **PR #354's Test Plan checkmarks are local-only, not exact-head CI evidence, and its branch cannot currently merge cleanly.** The PR body checks off `npm run check-breakpoint-contract` and `npm run breakpoint-boundaries` as passing, but its exact head has zero CI check-runs, and `gh pr view 354` reports `mergeable: CONFLICTING`, `mergeStateStatus: DIRTY` against current `main`. Treat this PR's evidence as unverified by CI and currently blocked. #354 duplicates already-merged #351's canonical guard rather than extending it, and remains unmerged as of this snapshot.
2. **Outdated: `axe-core` dependency claim.** Earlier snapshots noted that `axe-core` was a declared dependency but not called by any merged journey. `e2e/shell-accessibility-coverage.js` (merged via #352, `91c5e2a`) now actively calls `axe.run()` across five shell surfaces. The broader axe gate for Setup landings/Home/Tasks remains unmerged and is tracked under #359.
4. **`ROADMAP.md`'s "Currently active sequencing" section still describes #345 as one undifferentiated deliverable** ("guided Setup, seven-area IA, accessibility, and operator validation completion (#345)") and does not yet reflect the batch split now visible across #347/#349/#350/#351/#352/#353/#354. This is not necessarily wrong (the batches are all still "part of #345"), but a reader relying on `ROADMAP.md` alone would not learn that #345 has already been split into seven-plus tracked pieces, three of them merged. Flagged for the owner's awareness; not fixed here per this task's scope boundary (`ROADMAP.md` is explicitly out of scope for this PR).

**Superseded findings removed from the stale claims list**: 
- Round 2 listed "PR #347 is based on an older `main` commit" — #347 has 
  since merged, so base-branch staleness no longer applies.
- Earlier snapshots called #352/#353 unmerged with open blockers — both are 
  now merged with all cited blockers resolved. Do not read obsolete PR heads 
  (`746c823e4`, `f2a0e554`, `057e9e8`) as current in non-history sections of 
  this document.

These false statements are removed rather than kept in a "stale claims" list, 
which would itself become another false claim when it outdates. The freshness 
rule above exists to catch exactly this kind of drift.

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

---

## Planned/open tracking issues (not completion evidence)

The following issues track remaining work as of this snapshot. They are
recorded for roadmap visibility only, never as implementation evidence:

- **#357**: Refresh PR #355's acceptance ledger after merges (this task)
- **#358**: Seven-area information architecture IA and navigation restructure
- **#359**: Broader automated WCAG coverage (axe-core across all affected surfaces)
- **#360**: League-context HTTP transport and context-bar UI integration

None of these have merged implementation. Criterion 3, 4 (UI portion), 5, 7
(manual passes and broader axe), 8, and #354's remaining breakpoint work are
all mapped to these tracked issues or remain explicitly unaddressed.

---

## Snapshot semantics

This ledger is a **timestamped snapshot** of #345's acceptance state as of
`2026-07-27T13:45:00Z`, based on `main` @ `9447bb6`. It is accurate for
that exact SHA and time only.

**#345's issue checklist and current GitHub state remain authoritative for
later merge readiness.** If any of the following occur after this snapshot,
this document's status values become stale:

- #354 (Breakpoint consolidation) advances, is rebased, or is merged.
- Issues #357–#360 advance or are closed without the implementation they track.
- New active PRs are opened for #345 work.
- The seven acceptance boxes on #345's issue body are modified.
- Other cited merge commits or PRs are force-pushed or revert their changes.

Do not treat a stale snapshot as authoritative for merge gates. Instead,
re-read the live GitHub state and refresh this document before relying on
it again.
