-- Referential integrity for the physical facility hierarchy: a rink belongs to
-- a real venue, an ice slot to a real rink, a game's slot to a real ice slot,
-- and a season's venue access to a real venue (#201 Slice 3 — the IceSlot /
-- venue "no row lock" work the Slice 2 gate deliberately deferred).
--
-- These are the concurrency backstops for the reviewer-catalogued races that no
-- row lock covers today (Venue/Rink/IceSlot expose no get_*_for_update):
--   * create_game vs delete_ice_slot   → games.ice_slot_id → ice_slots(id)
--   * grant_season_venue_access vs delete_venue → season_venue_access.venue_id → venues(id)
--   * create_rink vs delete_venue       → rinks.venue_id → venues(id)
--   * create_ice_slot vs delete_rink    → ice_slots.rink_id → rinks(id)
-- Each service op validates its destination parent up front, but under
-- PostgreSQL READ COMMITTED a writer that read the parent as present can still
-- commit after a concurrent delete removed it; the foreign key is the database
-- backstop that rejects that orphaning write regardless of interleaving, so no
-- rink is stranded on a deleted venue, no slot on a deleted rink, no game on a
-- deleted slot, and no venue-access row on a deleted venue.
--
-- All four columns stay NULLABLE (a nullable foreign key still permits NULL;
-- season_venue_access.venue_id is independently NOT NULL from migration 029);
-- only a concrete id must now name a row that exists. A racing write's violation
-- is translated at the store boundary into a stable, secret-free domain conflict
-- (venue_not_found / rink_not_found / ice_slot_not_found), not a raw driver
-- error.
--
-- Cross-Season ice-slot double-booking (create_game in two different Seasons
-- targeting the same slot, which lock different Season rows and don't serialize)
-- is ALREADY DB-enforced by migration 022's partial unique index
-- ux_games_active_ice_slot; this slice adds the store-boundary translation of
-- that violation into a stable ScheduleConflictError (ice_slot_taken), so the
-- race loser sees the same conflict the service pre-check raises. No new
-- constraint is needed for it.
--
-- The season side of season_venue_access is intentionally NOT given a foreign
-- key here: grant_season_venue_access and delete_season both take the Season row
-- lock (get_season_for_update), so that pair already serializes and cannot
-- orphan; only the venue side lacks a lock and needs the backstop. Keeping
-- season_venue_access to a single outgoing foreign key also lets the SQLite
-- write-site disambiguate its one FK violation unambiguously.
--
-- PostgreSQL enforces declared foreign keys natively, so four ADD CONSTRAINTs
-- are enough. (The SQLite variant rebuilds all four tables because SQLite cannot
-- add a foreign key to an existing table.) ON DELETE NO ACTION is spelled out
-- (not left to PostgreSQL's default) so the restrictive, non-cascading safety
-- contract is visible in the DDL: deleting a still-referenced venue, rink, or
-- ice slot is rejected, never cascaded.
--
-- A forward-only pre-migration check (store.integrity_checks:
-- assert_iceslot_venue_fks_ready) reports every rink whose non-null venue_id
-- names a missing venue, every ice slot whose non-null rink_id names a missing
-- rink, every game whose non-null ice_slot_id names a missing ice slot, and
-- every season_venue_access whose venue_id names a missing venue, before the
-- constraints are added — so an upgrade against dangling data fails loudly,
-- naming the rows, rather than with an opaque constraint error.
ALTER TABLE rinks
  ADD CONSTRAINT fk_rinks_venue
  FOREIGN KEY (venue_id) REFERENCES venues (id) ON DELETE NO ACTION;
ALTER TABLE ice_slots
  ADD CONSTRAINT fk_ice_slots_rink
  FOREIGN KEY (rink_id) REFERENCES rinks (id) ON DELETE NO ACTION;
ALTER TABLE games
  ADD CONSTRAINT fk_games_ice_slot
  FOREIGN KEY (ice_slot_id) REFERENCES ice_slots (id) ON DELETE NO ACTION;
ALTER TABLE season_venue_access
  ADD CONSTRAINT fk_sva_venue
  FOREIGN KEY (venue_id) REFERENCES venues (id) ON DELETE NO ACTION;
