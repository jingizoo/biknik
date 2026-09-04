# Destructive Subtree Inventory and Preview Contract

> **Status:** the reviewed ownership inventory and pure preview landed through
> PR #448. This follow-up branch adds the separate high-privilege execution
> slice required by issue #429: durable single-use challenges, atomic execution,
> survivor audit, authenticated routes, and the preview/confirmation UI.
> Ordinary delete endpoints remain dependency-gated and acquire no cascade.

## Why the inventory landed before execution

`Delete subtree` is an intentional exception to the application's normal
delete-one-record contract. The exception is safe only if the product can first
answer, for every persisted relationship:

1. Is the referring record owned by the deleted target and therefore removed?
2. Is it shared and therefore retained with only the edge detached?
3. Is it historical evidence which stays unchanged and cannot grow the delete?
4. Does the field merely look like a relationship while actually holding an
   external key, actor attribution, trace id, or opaque snapshot?

The executable inventory in
`services/subtree_preview.py::REFERENCE_INVENTORY` is the answer. A contract
test derives every persisted entity from `SqlStore.SPECS`, derives every current
reference-looking field from the model dataclasses, and requires an exact
bidirectional match. The same test inspects a freshly migrated SQLite schema and
requires every real foreign key to be a live catalogued edge. Adding a table,
`*_id`, `*_ref`, actor field, JSON carrier, durable `team_side`, per-recipient
`actor_key`, or the `ActiveContext.id` user link without classifying it fails the
suite.

This is deliberately stricter than documenting the database foreign keys. Many
current relationships are enforced by services or encoded in typed strings/JSON
and have no database FK on all three stores.

## Current ownership decisions

### Competition tree

- A Program owns its Seasons, permanent Leagues, and Teams.
- A LeagueSeason is the association between one permanent League and one
  Season. Removing either endpoint removes that association, while the other
  permanent endpoint survives.
- A LeagueSeason owns its Divisions and age-rule versions. Registrations,
  memberships, schedule scenarios, and Games are relationship/history records
  removed when any required owning endpoint is removed.
- A Team owns its Players. Team `club_id` is an optional affiliation: deleting a
  Club detaches the affiliation and preserves the Team. The old `division_id`
  field is historical compatibility data and cannot grow a subtree.
- A Division assignment on a SeasonTeamRegistration is nullable and shared.
  Deleting only a Division detaches that assignment; it does not erase the
  Team's Season registration.

### Facility tree and ice

- An Organization owns Venues; a Venue owns Rinks; a Rink owns IceSlots.
- `Program.operator_organization_id` is an optional operator relationship, not
  ownership of the Program. Deleting the Organization detaches that field and
  preserves the Program.
- SeasonVenueAccess is an association row. Removing a Season or Venue removes
  the access row while preserving the other endpoint.
- `Game.ice_slot_id` is shared inventory. Removing a Game removes its ice edge
  and preserves the slot. Removing a facility subtree may detach the live ice
  edge only from a genuine generated draft: `is_draft` true, unpublished,
  uncancelled, and without any inbound `DELETE_SOURCE` state owned by the Game.
  That axis is derived from the relationship inventory, so it includes results,
  roster entries, assignments, availability, substitute/reschedule work,
  notifications, and audit evidence without copying a second dependency list.
  The Game remains a draft and its stale rink display is cleared; the preview
  discloses that survivor transition as `draft_game_unplaced`.
- A committed, published, cancelled-but-still-attached, result-bearing, or
  otherwise historical Game blocks preview with
  `game_cancellation_required`. The operator must use the explicit #428
  cancellation command first, which snapshots the facility facts, preserves
  fixture history, and releases the live ice edge. Only a fresh subtree preview
  after cancellation may proceed. Subtree deletion never re-drafts or silently
  cancels a committed fixture.
- A retained allocated IceSlot is released only when no surviving active Game
  still occupies it. The preview discloses that transition as
  `ice_slot_released`; it is revalidated and applied in the same atomic
  transaction as the deletion. Every deleted Game whose reservation causes a
  release is Program-authorized from all of its competition parents and Teams;
  a Season-less exhibition therefore uses its Team Program, while a broken or
  cross-Program Game graph refuses before preview disclosure.
- `Venue.league_id` is legacy compatibility state, but it is still a live,
  nullable FK in the current schema. A Program subtree must therefore detach it;
  calling it historical and retaining it would make the destructive transaction
  fail.
- A cancelled Game's `cancelled_ice_slot_id`, `cancelled_venue_id`, and
  `cancelled_rink_id` are display snapshots created by #428, deliberately
  without foreign keys. They remain part of the Game state digest and never
  pull current facility inventory into a competition subtree. Deleting the
  cancelled Game removes those snapshots with the Game.

### Games, people, integrations, and history

- A Game is removed when its Season, League, LeagueSeason, Division, home Team,
  or away Team is removed. Its roster entries, availability rows, substitute
  rows, result, reschedule requests, official assignments, roster audit rows,
  notifications, recipients, and deliveries follow it.
- `GameRosterEntry.team_side` is durable Team attribution and is inventoried
  separately from `player_id`; deleting that Team cannot leave an attributed
  roster row behind.
- A Player deletion removes Player associations such as memberships, roster and
  availability rows, substitute rows, guardian links, player-addressed feeds,
  and integration destinations. The guardian UserAccount remains.
- User accounts are principals, not descendants of a Team/Player/Official.
  Their scope edge is detached by a subject subtree and the now-unbound account
  is deactivated in the same transaction. Sessions remain owned by the
  UserAccount and become unusable because session resolution re-reads that live
  account state. The preview discloses this retained-row transition as
  `user_account_deactivated`; re-binding/reactivating the account is an explicit
  recovery action, not an automatic consequence of undoing any other record.
- ActiveContext is a view preference, not authority. Program/Season/League
  selections are detached when their targets disappear; its primary key is also
  inventoried as the owning UserAccount link.
- SetupAuditLog and DataAccessLog references are historical evidence and do not
  pull their subjects into or follow them out of a subtree. Detailed roster
  AuditLog rows, by contrast, are Game-owned and leave with the Game.
- The destructive command appends a separate aggregate,
  server-attributed event outside the removed subtree. It must not copy protected
  child payloads into that survivor merely to preserve deleted detail.
- External import refs, generation/response snapshots, audit detail, and trace
  identifiers are classified explicitly as non-graph values. They affect their
  containing row's state fingerprint but are never traversed as current
  authority.

### Archived Seasons are a hard boundary

Archived Season state is read-only. Preview computes the deletion closure and
all planned detachments over the full projected graph before pruning it to the
selected subtree. If either the delete set or a retained source that would be
changed intersects an archived Season's dependent closure, preview refuses with
`season_archived`. This includes a facility-root operation whose Game or other
dependent record belongs to an archived Season even though the Season itself is
outside the selected facility tree. The Season must be reopened through its
ordinary lifecycle command before a new preview can be built.

### Root and survivor authority

The exact League Admin capability is necessary but is not authority over every
record in the installation. Preview applies the ordinary context and setup
target gates to the selected root before reading its name or descendants, so an
absent and an inaccessible root return the same `root_not_found` response.

Cascade ownership then comes from the inventory: exclusively deleted
descendants inherit the authorized root rather than requiring every Season in a
multi-Season Program to be the one active Season. That inheritance does not
extend to a surviving source row that #429 will mutate. Every such source of a
`DETACH` edge is batch-authorized through its ordinary target (including bridge
rows and a reschedule request's owning Game) before safety diagnostics or
preview output.

Every Venue in the deletion closure is the other explicit boundary, including
a directly selected Venue root. A deleted Rink applies the same rule to its
surviving parent Venue. All Venue links are derived through the canonical edge
resolver (legacy Program link plus active or revoked SeasonVenueAccess). An
unlinked descendant inherits the authorized Organization; an unlinked root must
still pass its ordinary root gate. A linked Venue may span Seasons inside the
explicitly active Program, but any foreign or dangling Program link refuses the
whole preview. This is deliberately Program-axis-only so a facility subtree is
not tied to one active Season.

An Official is likewise a shared identity: deleting one removes every
OfficialAssignment. Its complete home-Club and assigned-Game Program union must
therefore be the explicitly active Program. Assignments in another Season of
that same Program are allowed; any foreign or unresolvable Club/Game link
refuses. Principal-bound UserAccount and ActiveContext effects remain under the
route's exact `manage_users` requirement. Execution repeats the same boundary
batch under the graph lock. Any refusal collapses to the root's generic
not-found response and creates no challenge.

## Pure preview contract

The authenticated store projector supplies:

- one selected root record;
- the root's operator-visible confirmation name;
- every record in the affected closure, represented only by entity type, stable
  operator-safe id, and a SHA-256 digest of all material row state;
- every resolved live relationship, naming one inventory entry and exact source
  and target records;
- the trusted authenticated actor id.

`build_subtree_preview` validates all records and edges before planning. Unknown
relationship keys, mismatched source/target types, a non-graph field presented as
an edge, duplicate records/edges, missing endpoints, unsafe identifiers, invalid
state digests, or a root whose state differs from the graph all fail closed.

Planning starts with the selected root and repeatedly includes the source of
every `DELETE_SOURCE` edge whose target is already being deleted. Once the
closure is stable:

- every relationship whose source is deleted is listed under removed edges;
- every `DETACH` edge into a deleted target is listed under detached edges and
  its source record is listed as retained;
- every historical `RETAIN` edge into a deleted target is listed separately and
  its source record is listed as retained;
- every surviving endpoint referenced by a deleted row is named as retained.

The output contains grouped counts and sorted stable ids for deleted and retained
records plus removed/detached/retained relationships grouped by inventory key,
with their own derived counts and sorted edge descriptions. It also contains
`retained_change_groups`, each with exactly `effect`, `entity_type`, `count`, and
sorted `record_ids`. The current effects are `draft_game_unplaced`,
`user_account_deactivated`, and `ice_slot_released`. These groups are derived
from the same plan that execution will apply and are rendered before typed
confirmation. A row can appear in both `retained_groups` and a retained-change
group: retained means "not deleted", not "byte-for-byte unchanged". The output
contains no domain model, row payload, state digest, contact destination,
account scope, or free-form child label.

## Fingerprint and stale-preview rule

The preview fingerprint is SHA-256 over canonical JSON containing:

- graph and execution contract versions;
- trusted actor id;
- selected root type/id/state digest;
- normalized typed-confirmation name;
- every projected record type/id/state digest;
- every projected relationship inventory key and endpoint;
- every disclosed retained change as its effect, entity type, and sorted record
  ids.

Input ordering has no effect. A material row change, relationship change, new
owned child, changed actor, changed root, changed confirmation name, or changed
retained survivor effect changes the fingerprint. A deployment that changes the
execution contract version also invalidates older challenges. Execution
re-projects under transaction locks and requires byte equality before the first
write. A mismatch returns a stable `preview_stale` refusal and applies no
partial mutation.

The fingerprint becomes usable only through a short-lived cryptographically
random challenge. Only its SHA-256 digest is stored, one active challenge per
actor. Preview replacement is atomic; a guessed or older token cannot consume a
newer legitimate preview. Execute rejects expiry/replay, re-projects under the
graph locks, and requires byte equality before the first destructive write. It
also re-checks the exact League Admin role plus `manage_setup` and
`manage_users`, the exact parent name, and a bounded non-empty audit reason.

## Execution slice added by this branch

The separate execution path now provides:

1. An authenticated store projector for every named allowed root type, with
   privacy-filtered identifiers and material row digests.
2. A durable preview challenge and aggregate survivor audit record, with a
   forward-only Memory/SQLite/PostgreSQL-compatible migration.
3. One deterministic lock and authorization order. After request-shape checks
   and a non-consuming challenge inspection, execution:
   1. re-reads the live actor as League Admin and acquires the explicit
      ActiveContext mutation lock;
   2. locks the full graph-table set in deterministic order, using PostgreSQL
      `EXCLUSIVE NOWAIT` so contention returns a retryable refusal instead of
      waiting into a graph-lock cycle;
   3. locks and authorizes the selected root under that graph lock, using the
      same generic not-found response for an absent or inaccessible root;
   4. consumes only the exact matching challenge;
   5. re-projects, batch-authorizes every surviving setup row that would be
      detached, and compares the fingerprint before applying detaches,
      retained-row state changes, child-first deletion, and the aggregate audit
      in one atomic destructive transaction.

   Challenge consumption, survivor changes, child-first deletion, and the audit
   commit atomically. A typed-name mismatch, changed context,
   missing/inaccessible target, graph-lock contention, `preview_stale`, or any
   later execution-stage failure leaves both the domain and the exact challenge
   unchanged. A retry re-runs live identity, context, target, graph, and
   fingerprint validation. A successful commit consumes the challenge; a
   replacement preview supersedes only that actor's older challenge.
4. A route/permission contract which does not widen ordinary delete endpoints.
5. Memory/SQLite/PostgreSQL failure injection and concurrent child creation,
   reassignment, cancellation, slot allocation, and competing-delete coverage.
6. Desktop and 390px UI with keyboard-only operation, deliberate focus movement,
   accessible warnings, typed confirmation, loading/error/retry states, and no
   console errors. Preview refusal states say explicitly that no new preview or
   challenge was created by that attempt. Confirmation lists retained survivor
   changes before the destructive action, and success distinguishes rows deleted
   from retained rows whose disclosed link/state changes were applied.

The feature remains unmerged and therefore not deployable until exact-head
review and required CI finish. The operation is reachable only as a separately
named action after an ordinary delete returns its dependency breakdown.
