# Destructive Subtree Inventory and Preview Contract

> **Status:** bounded, non-production design/code slice for issue #429. It
> inventories the current persisted ownership graph and defines a pure preview.
> It does **not** add a destructive command, route, token store, migration, UI,
> authorization rule, audit write, or deletion. Ordinary delete endpoints remain
> dependency-gated and acquire no cascade.

## Why this lands before execution

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
  and preserves the slot. Removing a facility subtree preserves an outside Game
  and detaches its live ice edge. The future execution slice must define the
  resulting Game state and release semantics under the same transaction before
  it wires that path.
- `Venue.league_id` is legacy compatibility state, but it is still a live,
  nullable FK in the current schema. A Program subtree must therefore detach it;
  calling it historical and retaining it would make the future delete fail.
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
  Their scope edge is detached by a subject subtree; the future execution slice
  must deactivate or safely rebind the account in the same transaction. Sessions
  remain owned by the UserAccount.
- ActiveContext is a view preference, not authority. Program/Season/League
  selections are detached when their targets disappear; its primary key is also
  inventoried as the owning UserAccount link.
- SetupAuditLog and DataAccessLog references are historical evidence and do not
  pull their subjects into or follow them out of a subtree. Detailed roster
  AuditLog rows, by contrast, are Game-owned and leave with the Game.
- The future destructive command must append a separate aggregate,
  server-attributed event outside the removed subtree. It must not copy protected
  child payloads into that survivor merely to preserve deleted detail.
- External import refs, generation/response snapshots, audit detail, and trace
  identifiers are classified explicitly as non-graph values. They affect their
  containing row's state fingerprint but are never traversed as current
  authority.

## Pure preview contract

The future authenticated store projector supplies:

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
with their own derived counts and sorted edge descriptions. It contains no
domain model, row payload, state digest, contact destination, account scope, or
free-form child label.

## Fingerprint and stale-preview rule

The preview fingerprint is SHA-256 over canonical JSON containing:

- contract version;
- trusted actor id;
- selected root type/id/state digest;
- normalized typed-confirmation name;
- every projected record type/id/state digest;
- every projected relationship inventory key and endpoint.

Input ordering has no effect. A material row change, relationship change, new
owned child, changed actor, changed root, or changed confirmation name changes
the fingerprint. The future execution command must re-project under its
deterministic transaction locks and require byte equality before the first
write. A mismatch returns a stable `preview_stale` refusal and consumes no
partial work.

The fingerprint is not yet a capability token. The execution slice must store a
short-lived hash of a cryptographically random, single-use token bound to this
fingerprint and actor, consume it atomically, and reject expiry/replay. It must
also require high-privilege server authorization and exact typed confirmation;
none of those guarantees can be inferred from calling this pure function.

## Required next slice before production

After this inventory is reviewed, destructive execution still needs all of the
following in one bounded feature slice:

1. An authenticated store projector for every allowed root type, with
   privacy-filtered identifiers and material row digests.
2. A durable preview challenge and aggregate survivor audit record, with a
   forward-only Memory/SQLite/PostgreSQL-compatible migration.
3. One deterministic lock order and a single atomic transaction covering graph
   revalidation, edge detachment, slot-state updates, child-first deletion,
   audit append, token consumption, and rollback.
4. A route/permission contract which does not widen ordinary delete endpoints.
5. Memory/SQLite/PostgreSQL failure injection and concurrent child creation,
   reassignment, cancellation, slot allocation, and competing-delete coverage.
6. Desktop and 390px UI with keyboard-only operation, deliberate focus movement,
   accessible warnings, typed confirmation, loading/error/retry states, and no
   console errors.

Until those land and pass review, this slice is neither deployable nor a partial
implementation of `Delete subtree`.
