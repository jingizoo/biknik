-- Referential integrity for the competition/facility ownership hierarchy above
-- the physical facility layer: a Program is operated by a real Organization, a
-- Season and a League belong to a real Program, and a Venue's owning
-- Organization and legacy owning Program (Venue.league_id) name real rows (#201
-- Slice 4 — the Program/Organization + Venue-owner "no row lock" work carved out
-- of Slice 3, jingizoo/biknik#201).
--
-- These are the concurrency backstops for the create-child-vs-delete-parent
-- races that no row lock covers today (Organization/Program expose no
-- get_*_for_update):
--   * create_program vs delete_organization → programs.operator_organization_id → organizations(id)
--   * create_venue   vs delete_organization → venues.organization_id → organizations(id)
--   * create_season  vs delete_program      → seasons.program_id → programs(id)
--   * create_league  vs delete_program      → leagues.program_id → programs(id)
--   * create_venue   vs delete_program      → venues.league_id → programs(id)
-- Each service op validates its destination parent up front, but under
-- PostgreSQL READ COMMITTED a writer that read the parent as present can still
-- commit after a concurrent delete removed it; the foreign key is the database
-- backstop that rejects that orphaning write regardless of interleaving, so no
-- program is stranded on a deleted organization, no season/league/venue on a
-- deleted program, and no venue on a deleted organization.
--
-- Every one of these columns is NULLABLE (operator_organization_id,
-- venues.organization_id and venues.league_id are optional owner links;
-- seasons.program_id and leagues.program_id always carry a concrete id in
-- practice but are not declared NOT NULL, and this slice does not change that).
-- A nullable foreign key still permits NULL and still rejects a concrete id that
-- names no row — only orphaning writes are blocked. A racing write's violation
-- is translated at the store boundary into a stable, secret-free domain conflict
-- (organization_not_found / program_not_found), not a raw driver error.
--
-- venues carries TWO outgoing foreign keys here (organization_id → organizations
-- and league_id → programs). PostgreSQL reports the exact violated constraint on
-- the error's diagnostics, so the store write-site disambiguates which parent
-- went missing by constraint name. (SQLite names neither, so the SQLite store
-- path disambiguates by re-reading which validated parent is now absent.)
--
-- ON DELETE NO ACTION is spelled out (not left to PostgreSQL's default) so the
-- restrictive, non-cascading safety contract is visible in the DDL: deleting a
-- still-referenced organization or program is rejected, never cascaded — the
-- service turns that rejection into the itemized has-dependencies block.
--
-- A forward-only pre-migration check (store.integrity_checks:
-- assert_program_org_fks_ready) reports every program whose non-null
-- operator_organization_id names a missing organization, every venue whose
-- non-null organization_id names a missing organization, every season/league
-- whose non-null program_id names a missing program, and every venue whose
-- non-null league_id names a missing program, before the constraints are added —
-- so an upgrade against dangling data fails loudly, naming the rows, rather than
-- with an opaque constraint error.
ALTER TABLE programs
  ADD CONSTRAINT fk_programs_operator_org
  FOREIGN KEY (operator_organization_id) REFERENCES organizations (id)
  ON DELETE NO ACTION;
ALTER TABLE venues
  ADD CONSTRAINT fk_venues_organization
  FOREIGN KEY (organization_id) REFERENCES organizations (id)
  ON DELETE NO ACTION;
ALTER TABLE seasons
  ADD CONSTRAINT fk_seasons_program
  FOREIGN KEY (program_id) REFERENCES programs (id) ON DELETE NO ACTION;
ALTER TABLE leagues
  ADD CONSTRAINT fk_leagues_program
  FOREIGN KEY (program_id) REFERENCES programs (id) ON DELETE NO ACTION;
ALTER TABLE venues
  ADD CONSTRAINT fk_venues_program
  FOREIGN KEY (league_id) REFERENCES programs (id) ON DELETE NO ACTION;
