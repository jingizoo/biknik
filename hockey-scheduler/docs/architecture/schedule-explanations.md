# Schedule preview explanations (#206 child #379)

`POST /api/scheduler/draft` keeps every existing response field and adds an
`explanation` value object to each `unscheduled[]` regular-season pairing. The
object is read-only preview evidence: it does not create or alter Games, select
different pairings/ice, publish, persist a scenario, or change commit behavior.

## Contract

`value_object_version` is `1`. The additive object contains:

- `blocking_constraint_codes`: canonical unique codes observed for the pairing;
- `candidate_windows`: a deterministic sample of actual, Season-accessible,
  available Game-ice inventory considered by the preview, with one or more
  machine-readable `rejections` per record;
- `alternatives`: bounded machine-readable input/policy corrections derived
  from those observed codes; and
- `bounds`: the configured caps plus actual/omitted counts.

The legacy `reason`, `reason_codes`, and `team_conflicts` fields retain their
existing meanings. In particular, legacy constraint evaluation still reports
the first cause in its established priority. The explanation observer runs only
after the pairing is known to be unplaced and may report simultaneous causes;
an internal observer-off golden test proves its presence cannot change chosen
fixtures or unplaced-pairing membership.

Example (ids and timestamps abbreviated):

```json
{
  "home_team_id": "t0",
  "away_team_id": "t1",
  "reason_codes": ["season_blackout"],
  "explanation": {
    "value_object_version": 1,
    "blocking_constraint_codes": [
      "season_blackout",
      "team_blackout",
      "rink_blackout"
    ],
    "candidate_windows": [
      {
        "ice_slot_id": "slot-42",
        "rink_id": "rink-2",
        "venue_id": "venue-1",
        "start_time": "2026-10-05T18:00:00+00:00",
        "end_time": "2026-10-05T19:00:00+00:00",
        "rejections": [
          {"code": "season_blackout", "details": {"date": "2026-10-05"}},
          {
            "code": "team_blackout",
            "details": {"date": "2026-10-05", "team_ids": ["t0"]}
          },
          {
            "code": "rink_blackout",
            "details": {"date": "2026-10-05", "rink_id": "rink-2"}
          }
        ]
      }
    ],
    "alternatives": [
      {
        "action_code": "review_season_blackout",
        "reason_code": "season_blackout",
        "season_id": "season-1",
        "date": "2026-10-05"
      },
      {
        "action_code": "review_team_blackout",
        "reason_code": "team_blackout",
        "team_ids": ["t0"],
        "date": "2026-10-05"
      },
      {
        "action_code": "review_rink_blackout",
        "reason_code": "rink_blackout",
        "rink_id": "rink-2",
        "date": "2026-10-05"
      }
    ],
    "bounds": {
      "candidate_window_limit_per_pairing": 8,
      "candidate_window_limit_per_preview": 128,
      "rejection_limit_per_candidate": 8,
      "alternative_limit_per_pairing": 3,
      "candidate_window_total": 1,
      "candidate_window_count": 1,
      "candidate_window_omitted_count": 0,
      "candidate_windows_truncated": false,
      "preview_candidate_budget_limited": false,
      "alternative_omitted_count": 0
    }
  }
}
```

## Stable order and explicit bounds

Candidate inventory is already sorted by `(start_time, ice_slot_id)` for the
greedy scheduler; evidence preserves that order. Rejection codes use the
planner's fixed evaluation order. Blocking codes and alternatives use the
documented canonical reason-code order in
`services/schedule_explanations.py`; unknown future codes sort lexically after
known codes.

- **8 candidates per pairing** gives an operator several dates/rinks to compare
  without repeating an entire Season's inventory on every unplaced matchup.
- **128 candidates per preview** caps both extra hard-constraint evaluation and
  the large evidence portion of the response. The budget is assigned in stable
  pairing order. Later pairings still receive blocking codes, alternatives, and
  explicit omitted counts when the shared budget is exhausted.
- **8 rejections per candidate** covers every current independent evaluation
  layer (request constraints, shared rink policy, and team overlap) while
  leaving explicit truncation room for future codes.
- **3 alternatives per pairing** prevents repeated remediation suggestions from
  becoming a second unbounded payload. `alternative_omitted_count` is honest
  when more observed corrections exist.

These are marginal explainability bounds. The pre-existing round-robin and
`unscheduled[]` row counts remain governed by the selected LeagueSeason and are
not changed by this slice.

## Authoritative and privacy-safe evidence

Only Game-type, initially AVAILABLE, unoccupied slots that pass the existing
SeasonVenueAccess scanner can enter `candidate_windows`. A slot selected by an
earlier proposed pairing is still real initial inventory but is recorded with
`ice_already_selected`; it is never offered as playable. Practice/blocked/used
inventory and another Season/Program's slots do not enter the candidate pool.
An explicit cross-Season slot selection continues to fail at the existing
server-side `venue_access_missing` boundary rather than becoming evidence.

Candidate evidence is allowlisted to stable ids, times, numeric policy facts,
and conflict references. It never passes through human error prose, Team/Rink/
Venue names, blackout notes, Player/guardian/contact/medical data, or arbitrary
operator-entered text. The allowlist applies to the ROWS of the two nested
evidence lists (`min_rest.conflicts`, `team_overlap.conflicts`) as well as to
the top-level detail fields — a nested dict is rebuilt from
`_SAFE_NESTED_ROW_FIELDS`, never copied. #390's `min_turnaround.conflicts`
is a third such list and is registered the same way — its rows carry only
identifiers and durations (team, blocking Game, that Game's own window, the
measured gap, the shortfall), it is capped at the same four rows with an
explicit `omitted_conflict_count`, and an unnameable blocking Game DROPS
`conflict_game_id` rather than nulling it, byte-identically to how the
team-overlap observer treats an out-of-context or not-yet-persisted Game.
The producer of the team-overlap rows
sits beside `_team_overlap_reason`, whose own conflict records carry
`team_name`, so "whatever today's producer passes" is not a safe rule. A Season with zero active venue-access grants receives
the concrete `review_season_venue_access` correction carrying only the selected
Season id; inaccessible venue/rink/slot identities remain hidden.

### A conflicting Game id is bound to the caller's own context

`#386`/`#388` bind every draft entry point to the caller's persisted active
tuple, and this evidence obeys the same boundary. The candidate SCANNER
already did: it never returns another Season's ice, so `ice_slot_id`,
`rink_id`, `venue_id` and the two timestamps are in-context by construction.

The active-game SNAPSHOT is the hole, and deliberately so.
`_active_game_slot_pairs` is unfiltered by `#373` — no Season, League,
Division, draft/published or GameType filtering — because the preview and the
commit gate must measure the same physical edges. Two Seasons holding active
access to one Venue is exactly what `SeasonVenueAccess` expresses, so a
neighbouring Season's Game routinely collides with in-context ice, and its id
reached the payload through three codes: `slot_overlap_conflict`,
`turnover_buffer_conflict` (via the rink) and `team_overlap` (via the team).

`_in_scope_game_ids` is therefore computed once per run from that same shared
snapshot and every conflicting Game id is checked against it. Season is a hard
conjunct for every Game. The League half depends on what the Game carries:

- a Game with an **owning League** is in scope only when that League matches —
  the two corners `#388` names, same Program/different Season and same
  Season/different League, are each refused a proposal and each would slip
  through a one-sided comparison;
- a **league-less** Game — an EXHIBITION, which carries no owning League at
  all by design (`#283` Slice D) — is in scope when at least one participating
  Team validates into the caller's League, per `#367`'s owner ruling.

That second branch is `#399`. Comparing an exhibition's `None` league id for
equality is not a boundary doing its job; it is a comparison that cannot
succeed, so a friendly played by the caller's own team, on the caller's own
ice, in the caller's own Season, blocked a placement whose explanation then
refused to name it — the operator was told the slot was taken and given no
Game to act on. The ruling's other half is what keeps this a correction rather
than an opening: an exhibition between two sibling-League teams is concretely
that League's fixture and stays withheld, names and all.

The Scheduler now answers this question exactly the way the Dashboard's
`_game_matches_active` does. An unresolved Season/League scope still withholds
every id rather than defaulting open, and an unresolvable participant is never
counted as validating into anything.

`conflict_slot_id` is NOT withheld. The candidate's Rink is in-context ice, so
a slot on that Rink is the caller's own inventory and the operator still
learns which of their windows is unusable and why. Only the Game id is
dropped, and it is dropped rather than nulled so the allowlist simply omits
the field.

Half of this was a restructuring of an existing disclosure — where the overlap
is the FIRST cause, the legacy `reason` prose already interpolates
`"...hosting game G on the same rink"`. The other half was new: the
established decision path short-circuits at its first cause, so a candidate
blocked by a blackout never reaches the policy advisory, while the all-causes
observer does. Both shapes are covered separately.

An alternative is always a correction, never a scheduling claim. Examples are
`review_team_blackout`, `review_minimum_rest_policy`,
`review_rink_scheduling_policy`, `reschedule_conflicting_game`, and
`increase_available_game_ice`. No alternative contains an `ice_slot_id` saying
that a hard-rejected window is playable. When the conflicting Game id is
withheld, `_alternative_for` degrades on its own from
`reschedule_conflicting_game` to `provide_non_overlapping_game_ice` — an
operator with no authority over the other Season cannot reschedule its Game,
so naming that action would have been the dishonest answer.

Team eligibility remains fail-closed before pair generation: inactive,
unregistered, cross-Program, and cross-League Teams do not become pairings and
therefore cannot appear in an unplaced pairing's evidence. Regular Games stay
inside their LeagueSeason/optional Division exactly as before.

## Falsifying mutations

Each row is an independent single-clause mutation of the source; each is killed
by the named test and by no other, and each was verified to leave the rest of
the suite green before its test existed.

| Mutation | Killed by |
| --- | --- |
| Drop the `holiday` clause from `_slot_constraint_rejections` | `test_simultaneous_blackout_causes_are_all_retained_and_identifier_only` |
| Reverse `candidates` in `build_unplaced_explanation` | `test_cap_order_and_scope_are_stable_across_multi_venue_inventory` |
| `MAX_CANDIDATE_WINDOWS_PER_PAIRING = 9` | `test_whole_preview_candidate_budget_stops_at_exact_boundary` |
| `ExplanationBudget.reserve` ignores `self.remaining` | `test_whole_preview_candidate_budget_stops_at_exact_boundary` |
| Drop the `[:MAX_REJECTIONS_PER_CANDIDATE]` slice | `test_rejection_and_alternative_caps_report_exact_omitted_counts` |
| Return every alternative and claim none was omitted | `test_rejection_and_alternative_caps_report_exact_omitted_counts` |
| `_season_scoped_slot_ids` scans every Season's ice | `test_zero_access_is_fail_closed_without_leaking_inaccessible_ice` |
| Delete the rejection-free-candidate guard in `_candidate_record` | `test_a_candidate_with_no_rejection_is_never_reported_as_evidence` |
| Let the observer add to `used` | `test_explanation_toggle_cannot_change_scheduler_decisions` |
| Drop `_annotate_missing_venue_access` | `test_zero_access_is_fail_closed_without_leaking_inaccessible_ice` |
| Drop the `pairs.sort` in `_active_game_slot_pairs` | `test_active_game_snapshot_is_canonical_not_store_order` |
| Ignore `legacy_reason_codes` | `test_no_available_ice_has_input_correction_not_invented_slot` |
| `candidate_window_omitted_count` hard-coded to 0 | `test_cap_order_and_scope_are_stable_across_multi_venue_inventory` |
| Replace `_SAFE_DETAIL_FIELDS` with a pass-through | `test_candidate_details_are_an_allowlist_not_a_pass_through` |
| Copy nested `conflicts` rows verbatim instead of allowlisting them | `test_the_allowlist_reaches_inside_nested_evidence_rows` |
| Sort blocking codes alphabetically instead of by rank | `test_blocking_codes_use_the_canonical_rank_not_the_alphabet`, `test_an_unknown_future_code_sorts_after_every_known_one` |
| Add `explanation` to the `_draft_fingerprint` allowlist | `test_explanation_stays_out_of_the_commit_preview_fingerprint` |
| Report ice spent by an earlier pairing as `no_ice_available` | `test_ice_taken_by_an_earlier_pairing_says_so_not_no_ice_available` |
| Remove `_in_scope_game_ids`' fail-closed branch | `test_an_unresolved_scope_withholds_every_id` |
| Compare only the League, not the Season | `test_a_teams_other_season_game_is_named_only_inside_the_tuple`, `test_only_the_exact_season_and_league_pair_is_in_scope` |
| Compare only the Season, not the League | `test_a_sibling_leagues_game_in_this_season_is_withheld_too`, `test_only_the_exact_season_and_league_pair_is_in_scope` |
| Restore the pre-`#399` `season AND league` conjunct for every Game | `test_an_active_season_exhibition_blocker_is_named`, `test_a_league_less_game_is_in_scope_only_via_a_participant` |
| Admit every league-less Game in the Season | `test_a_sibling_leagues_own_exhibition_is_still_withheld`, `test_a_league_less_game_is_in_scope_only_via_a_participant` |
| Skip the Season conjunct for league-less Games | `test_a_neighbouring_seasons_exhibition_is_withheld_too`, `test_a_league_less_game_still_obeys_the_season_conjunct` |
| Let `_plays_in_league` fail OPEN on a missing Team | `test_a_league_less_game_is_in_scope_only_via_a_participant`, `test_a_sibling_leagues_own_exhibition_is_still_withheld` |
| Keep the policy `conflict_game_id` unconditionally | `test_a_neighbouring_seasons_game_is_never_named_in_evidence` |
| Keep the `team_overlap` `conflict_game_id` unconditionally | `test_a_teams_other_season_game_is_named_only_inside_the_tuple` |
| Stop seeding the scope from the Division entry point | `test_an_in_scope_conflicting_game_is_still_named` (`shape='division'`) |
| Stop seeding the scope from the League-wide entry point | `test_an_in_scope_conflicting_game_is_still_named` (`shape='league-wide'`) |
| Uncap `min_rest.conflicts` (`kept = conflicts`) | `test_nested_evidence_rows_are_capped_and_count_what_they_dropped` |
| Copy `min_turnaround.conflicts` rows verbatim | `test_nested_conflict_rows_are_allowlisted_and_capped` (`test_scheduler_turnaround`) |
| Uncap `min_turnaround.conflicts` | `test_conflicts_are_capped_at_four_with_an_omitted_count` (`test_scheduler_turnaround`) |
| Keep the `min_turnaround` `conflict_game_id` unconditionally | `test_out_of_scope_game_ids_never_reach_the_payload` (`test_scheduler_turnaround`) |
| Drop the per-team `break` in `_team_overlap_reason` | `test_team_overlap_reports_at_most_one_row_per_team` |
| Delete `_authorize_schedule_target` from `draft_season_schedule` | `test_no_candidate_evidence_is_built_for_a_refused_target` (`test_draft_context_scope`) |

## Ownership boundary

This value object may be snapshotted opaquely by the scenario work. Scenario
naming/persistence, generation fingerprints, stale/atomic commit refusal, and
transactions belong to #378 and are intentionally unchanged here. Configurable
meeting counts, existing/cancelled/exhibition counting, deterministic home/away,
and regeneration/idempotence belong to #375 and are also unchanged.
The `min_turnaround` code registers itself in this module's existing tables
(`REASON_CODE_ORDER` next to `min_rest`, `_SAFE_DETAIL_FIELDS`,
`_SAFE_NESTED_ROW_FIELDS`, and one `_alternative_for` branch yielding
`review_minimum_turnaround_policy`); the turnaround RULE itself belongs to
#390 and lives in `services/scheduler.py`.

### `explanation` is deliberately OUT of `draft_fingerprint`

`_draft_fingerprint` (#328) binds an explicit, named allowlist of
`unscheduled[]` fields — pairing, names, `reason`, `reason_codes`, and
`team_conflicts` — and the commit gate refuses (`preview_stale`) when a fresh
regeneration disagrees with the reviewed one. `explanation` is **not** added to
that allowlist, and that is a decision rather than an omission: this observer
reads deeper into live state than the bound fields do (which conflicting Game a
shared-rink policy names first, and which candidate windows the shared
128-window preview budget happened to pay for), so binding it would let an
unrelated neighbouring Game turn a batch whose placements, reasons, and team
conflicts are byte-for-byte identical into a spurious refusal. The scenario
record's own `proposal_fingerprint` still covers the whole proposal including
`explanation`, because that one is a tamper check over an immutable stored
record rather than a staleness check against a regeneration — which is exactly
why this object has to be deterministic across processes and store backends.
`test_explanation_stays_out_of_the_commit_preview_fingerprint` pins both halves;
without it, binding `explanation` into the fingerprint passes the entire suite.
