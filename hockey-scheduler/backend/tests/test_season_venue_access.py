"""SeasonVenueAccess — the Season<->Venue join (#233 Slice E, E1).

Physical structure (Facility Organization -> Venue -> Rink -> Ice Slot) and
competition structure (Program -> Season -> League -> optional Division) are
independent trees; this is the explicit many-to-many join between them,
replacing the permanent Venue.league_id bridge. Covers: grant/revoke/list
through the service+facade, the reactivate-in-place rule (mirroring
register_team_for_season's Rule 5), not-found handling, the partial unique
index enforcing one active row per (season_id, venue_id) pair even on a raw
store-level race, and migration 029's deterministic backfill + ambiguity
abort. Runs on Memory, SQLite, and (when TEST_DATABASE_URL is set)
PostgreSQL.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Program, Season, Venue
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import MigrationDataError
from hockey_scheduler.store.sql_store import migrate

ACTOR = "user_admin"


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()  # isolate on a shared database
    return store


def _teardown(store):
    """Unconditionally return a (possibly SHARED, e.g. Postgres) database to a
    clean baseline before closing.

    Several tests below deliberately leave migration 029 un-recorded (to
    exercise the abort path) or insert raw fixture rows outside the normal
    create_* API — on a shared Postgres database that state would otherwise
    persist into the NEXT test's ``SqlStore(url)`` construction, whose
    constructor runs ``migrate()`` immediately and would re-hit the same
    abort before that test ever gets a chance to reset the schema itself.
    Mirrors test_competition_reset_migration.py's identical teardown."""
    try:
        store.reset_schema()
    finally:
        store.close()


class SeasonVenueAccessContract:
    """Store-agnostic service+facade behavior; concrete cases pick the store."""

    def setUp(self):
        self.store = self.make_store()
        self.api = ApiService(self.store)

    def _fixture(self):
        org = self.api.create_organization("Org", actor_id=ACTOR)["id"]
        program = self.api.create_program(
            "Prog", operator_organization_id=org, actor_id=ACTOR)["id"]
        season = self.api.create_season(program, "S1", actor_id=ACTOR)["id"]
        venue = self.api.create_venue(
            "V1", organization_id=org, actor_id=ACTOR)["id"]
        return org, program, season, venue

    def test_grant_creates_active_access_with_audit(self):
        _, _, season, venue = self._fixture()
        audits_before = len(self.store.all_setup_audit())
        result = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["season_id"], season)
        self.assertEqual(result["venue_id"], venue)
        self.assertTrue(result["active"])
        audits = [a for a in self.store.all_setup_audit()
                  if a.action == "season_venue_access_granted"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].entity_id, result["id"])
        self.assertEqual(audits[0].actor_id, ACTOR)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before + 1)

    def test_duplicate_active_grant_is_rejected(self):
        _, _, season, venue = self._fixture()
        self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        dup = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.assertEqual(dup["error"]["code"], "validation_error")
        self.assertEqual(dup["error"]["details"]["reason"], "already_active")

    def test_grant_unknown_season_or_venue_not_found(self):
        _, _, season, venue = self._fixture()
        missing_season = self.api.grant_season_venue_access(
            "nope_season", venue, actor_id=ACTOR)
        self.assertEqual(missing_season["error"]["code"], "not_found")
        missing_venue = self.api.grant_season_venue_access(
            season, "nope_venue", actor_id=ACTOR)
        self.assertEqual(missing_venue["error"]["code"], "not_found")

    def test_revoke_deactivates_and_audits(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        revoked = self.api.revoke_season_venue_access(
            granted["id"], actor_id=ACTOR)
        self.assertNotIn("error", revoked, revoked)
        self.assertFalse(revoked["active"])
        audits = [a for a in self.store.all_setup_audit()
                  if a.action == "season_venue_access_revoked"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].entity_id, granted["id"])

    def test_revoke_already_revoked_is_rejected(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        again = self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(again["error"]["code"], "validation_error")
        self.assertEqual(again["error"]["details"]["reason"], "already_revoked")

    def test_revoke_missing_id_not_found(self):
        result = self.api.revoke_season_venue_access("nope", actor_id=ACTOR)
        self.assertEqual(result["error"]["code"], "not_found")

    def test_regrant_after_revoke_reactivates_same_row(self):
        # Mirrors register_team_for_season's Rule 5: a prior inactive row is
        # reactivated in place rather than duplicated.
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        audits_before = len(self.store.all_setup_audit())
        regranted = self.api.grant_season_venue_access(
            season, venue, actor_id=ACTOR)
        self.assertNotIn("error", regranted, regranted)
        self.assertEqual(regranted["id"], granted["id"])
        self.assertTrue(regranted["active"])
        # Exactly one new row, not a duplicate.
        rows = self.api.list_season_venue_access(season)["venue_access"]
        self.assertEqual(len(rows), 1)
        audits = [a for a in self.store.all_setup_audit()
                  if a.action == "season_venue_access_granted"]
        self.assertEqual(len(audits), 2)  # original grant + reactivation
        self.assertTrue(audits[-1].detail.get("reactivated"))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before + 1)

    def test_list_returns_only_this_seasons_access_rows(self):
        org, program, season, venue = self._fixture()
        other_season = self.api.create_season(program, "S2", actor_id=ACTOR)["id"]
        other_venue = self.api.create_venue(
            "V2", organization_id=org, actor_id=ACTOR)["id"]
        self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.grant_season_venue_access(season, other_venue, actor_id=ACTOR)
        self.api.grant_season_venue_access(other_season, venue, actor_id=ACTOR)
        rows = self.api.list_season_venue_access(season)["venue_access"]
        self.assertEqual({r["venue_id"] for r in rows}, {venue, other_venue})
        self.assertTrue(all(r["season_id"] == season for r in rows))

    def test_one_season_can_use_multiple_venues(self):
        org, _, season, venue = self._fixture()
        venue2 = self.api.create_venue(
            "V2", organization_id=org, actor_id=ACTOR)["id"]
        self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.grant_season_venue_access(season, venue2, actor_id=ACTOR)
        rows = self.api.list_season_venue_access(season)["venue_access"]
        self.assertEqual({r["venue_id"] for r in rows}, {venue, venue2})

    def test_one_venue_can_host_multiple_seasons_across_programs(self):
        # No Venue<->Program ownership constraint — the whole point of #233
        # Slice E — so a Venue may back Seasons under DIFFERENT Programs.
        org, program, season, venue = self._fixture()
        program2 = self.api.create_program(
            "Prog2", operator_organization_id=org, actor_id=ACTOR)["id"]
        season2 = self.api.create_season(program2, "S1", actor_id=ACTOR)["id"]
        r1 = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        r2 = self.api.grant_season_venue_access(season2, venue, actor_id=ACTOR)
        self.assertNotIn("error", r1, r1)
        self.assertNotIn("error", r2, r2)
        self.assertNotEqual(r1["id"], r2["id"])

    # -- Season/Venue delete dependency gate (#255 review) ------------------
    # SeasonVenueAccess must never be an orphan: delete_season/delete_venue
    # both treat a matching access row as a live dependent, active or not
    # (revoke_season_venue_access only deactivates — the grant/revoke
    # history is preserved by default), with zero mutation/audit on a
    # blocked attempt. delete_season_venue_access is the explicit,
    # separate, audited cleanup action for an already-revoked row.
    def test_delete_season_blocked_by_active_venue_access(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_season(season, actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in blocked["error"]["details"]["dependencies"]}
        self.assertIn("venue access", deps)
        self.assertIsNotNone(self.store.get_season(season))
        self.assertTrue(self.store.get_season_venue_access(granted["id"]).active)
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_delete_venue_blocked_by_active_venue_access(self):
        _, _, season, venue = self._fixture()
        self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_venue(venue, actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in blocked["error"]["details"]["dependencies"]}
        self.assertIn("venue access", deps)
        self.assertIsNotNone(self.store.get_venue(venue))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_delete_season_still_blocked_by_revoked_venue_access(self):
        # Revoking is not enough — history is preserved by design, so the
        # inactive row must still block until explicitly cleaned up.
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        blocked = self.api.delete_season(season, actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        self.assertIsNotNone(self.store.get_season(season))
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))

    def test_delete_venue_still_blocked_by_revoked_venue_access(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        blocked = self.api.delete_venue(venue, actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        self.assertIsNotNone(self.store.get_venue(venue))

    def test_delete_season_succeeds_after_explicit_cleanup(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        cleaned = self.api.delete_season_venue_access(
            granted["id"], actor_id=ACTOR)
        self.assertNotIn("error", cleaned, cleaned)
        self.assertEqual(cleaned["id"], granted["id"])
        self.assertIsNone(self.store.get_season_venue_access(granted["id"]))
        deleted = self.api.delete_season(season, actor_id=ACTOR)
        self.assertNotIn("error", deleted, deleted)
        self.assertIsNone(self.store.get_season(season))

    def test_delete_venue_succeeds_after_explicit_cleanup(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        deleted = self.api.delete_venue(venue, actor_id=ACTOR)
        self.assertNotIn("error", deleted, deleted)
        self.assertIsNone(self.store.get_venue(venue))

    def test_delete_season_venue_access_rejects_active_row(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        result = self.api.delete_season_venue_access(
            granted["id"], actor_id=ACTOR)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(result["error"]["details"]["reason"], "access_active")
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))

    def test_delete_season_venue_access_missing_id_not_found(self):
        result = self.api.delete_season_venue_access("nope", actor_id=ACTOR)
        self.assertEqual(result["error"]["code"], "not_found")

    def test_delete_season_venue_access_audits(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        audits = [a for a in self.store.all_setup_audit()
                  if a.action == "season_venue_access_deleted"]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].entity_id, granted["id"])
        self.assertEqual(audits[0].detail["season_id"], season)
        self.assertEqual(audits[0].detail["venue_id"], venue)

    def test_delete_season_venue_access_returns_resolved_labels(self):
        org = self.api.create_organization("LabelOrg", actor_id=ACTOR)["id"]
        program = self.api.create_program(
            "LabelProg", operator_organization_id=org, actor_id=ACTOR)["id"]
        season = self.api.create_season(program, "Labelled Season", actor_id=ACTOR)["id"]
        venue = self.api.create_venue(
            "Labelled Venue", organization_id=org, actor_id=ACTOR)["id"]
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        cleaned = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertNotIn("error", cleaned, cleaned)
        self.assertEqual(cleaned["season_name"], "Labelled Season")
        self.assertEqual(cleaned["venue_name"], "Labelled Venue")

    # -- Permanent cleanup never erases live Game history (#257 review) -----
    # A revoked access row can be the ONLY explicit record of why a Game's
    # ice was allowed at this Venue for this Season. delete_season_venue_access
    # must never purge it out from under a draft, committed, cancelled, or
    # published Game — mirrors delete_season_team_registration's own
    # game-history guard (#251) exactly.
    def _playable_game(self, season, venue, home_name="Home", away_name="Away"):
        """A full scheduling chain on ``venue``'s ice: League, Division, two
        registered Teams, and a free ice slot. Returns
        (division_id, home_id, away_id, slot_id)."""
        league = self.api.create_league(season, "L", actor_id=ACTOR)["id"]
        division = self.api.create_division(
            season, "D", league_id=league, actor_id=ACTOR)["id"]
        club = self.api.create_club("Club", actor_id=ACTOR)["id"]
        home = self.api.create_team(club, division, home_name, actor_id=ACTOR)["id"]
        away = self.api.create_team(club, division, away_name, actor_id=ACTOR)["id"]
        self.api.register_team_for_season(season, home, division, actor_id=ACTOR)
        self.api.register_team_for_season(season, away, division, actor_id=ACTOR)
        rink = self.api.create_rink(venue, "R", actor_id=ACTOR)["id"]
        slot = self.api.create_ice_slot(
            rink, "2027-01-15T18:00:00+00:00", "2027-01-15T19:30:00+00:00",
            actor_id=ACTOR)["id"]
        return division, home, away, slot

    def test_cleanup_blocked_by_committed_game(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        division, home, away, slot = self._playable_game(season, venue)
        game = self.api.create_game(season, division, home, away, slot, actor_id=ACTOR)
        self.assertNotIn("error", game, game)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        audits_before = len(self.store.all_setup_audit())
        blocked = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in blocked["error"]["details"]["dependencies"]}
        self.assertIn("game", deps)
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))
        self.assertEqual(len(self.store.all_setup_audit()), audits_before)

    def test_cleanup_blocked_by_published_game(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        division, home, away, slot = self._playable_game(season, venue)
        game = self.api.create_game(season, division, home, away, slot, actor_id=ACTOR)
        self.api.publish_game(game["id"], actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        blocked = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))

    def test_cleanup_blocked_by_cancelled_game(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        division, home, away, slot = self._playable_game(season, venue)
        game = self.api.create_game(season, division, home, away, slot, actor_id=ACTOR)
        self.api.cancel_game(game["id"], actor_id=ACTOR)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        blocked = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))

    def _make_draft_game(self, season, division, home, away, slot):
        """A draft game on ``slot`` — created through the normal (committed)
        path, then flipped to draft directly at the store, matching this
        file's established direct-store-manipulation pattern for shaping
        state a facade call alone can't reach (season_id is a required,
        explicit create_game argument, so this keeps it set correctly,
        unlike a raw scheduler-commit call)."""
        game = self.api.create_game(season, division, home, away, slot, actor_id=ACTOR)
        self.assertNotIn("error", game, game)
        row = self.store.get_game(game["id"])
        row.is_draft = True
        self.store.save_game(row)
        return game["id"]

    def test_cleanup_blocked_by_draft_game(self):
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        division, home, away, slot = self._playable_game(season, venue)
        self._make_draft_game(season, division, home, away, slot)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        blocked = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(blocked["error"]["code"], "has_dependencies")
        self.assertIsNotNone(self.store.get_season_venue_access(granted["id"]))

    def test_cleanup_succeeds_once_the_game_is_gone(self):
        # A DRAFT game's own delete control removes it outright (unlike a
        # committed/published game, which only cancels) — proves the block
        # really does key off live Game rows, not just "any game ever
        # touched this venue+season", by clearing the dependency and
        # re-checking that the cleanup then succeeds.
        _, _, season, venue = self._fixture()
        granted = self.api.grant_season_venue_access(season, venue, actor_id=ACTOR)
        division, home, away, slot = self._playable_game(season, venue)
        game_id = self._make_draft_game(season, division, home, away, slot)
        self.api.revoke_season_venue_access(granted["id"], actor_id=ACTOR)
        still_blocked = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertEqual(still_blocked["error"]["code"], "has_dependencies")
        deleted_game = self.api.delete_game(game_id, actor_id=ACTOR)
        self.assertNotIn("error", deleted_game, deleted_game)
        cleaned = self.api.delete_season_venue_access(granted["id"], actor_id=ACTOR)
        self.assertNotIn("error", cleaned, cleaned)
        self.assertIsNone(self.store.get_season_venue_access(granted["id"]))


class MemoryAccessTest(SeasonVenueAccessContract, unittest.TestCase):
    def make_store(self):
        return InMemoryStore()


class DurableAccessTest(SeasonVenueAccessContract, unittest.TestCase):
    def make_store(self):
        url = os.environ.get("TEST_DATABASE_URL") or ":memory:"
        store = SqlStore(url)
        store.reset_schema()
        return store


class ConstraintEnforcementTest(unittest.TestCase):
    """The partial unique index holds on every SQL backend, even bypassing
    the service layer's own reactivate-in-place check (a cross-process race
    the application-level check alone can't serialize)."""

    def test_second_active_row_for_same_pair_is_a_translated_conflict(self):
        from hockey_scheduler.domain import SeasonVenueAccess
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="p1", name="P"))
                store.add_season(Season(id="s1", program_id="p1", name="S"))
                store.add_venue(Venue(id="v1", name="V"))
                with store.transaction():
                    store.add_season_venue_access(
                        SeasonVenueAccess(id="sva1", season_id="s1",
                                          venue_id="v1", active=True))
                with self.assertRaises(IntegrityConflictError, msg=label) as ctx:
                    with store.transaction():
                        store.add_season_venue_access(
                            SeasonVenueAccess(id="sva2", season_id="s1",
                                              venue_id="v1", active=True))
                self.assertEqual(
                    ctx.exception.details["reason"], "unique_violation", label)
            finally:
                _teardown(store)

    def test_two_inactive_rows_for_the_same_pair_are_allowed(self):
        # The partial index only constrains active=1 rows — historical
        # inactive rows for the same pair (e.g. from repeated grant/revoke
        # cycles before a hard-delete existed) don't collide.
        from hockey_scheduler.domain import SeasonVenueAccess
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="p1", name="P"))
                store.add_season(Season(id="s1", program_id="p1", name="S"))
                store.add_venue(Venue(id="v1", name="V"))
                with store.transaction():
                    store.add_season_venue_access(
                        SeasonVenueAccess(id="sva1", season_id="s1",
                                          venue_id="v1", active=False))
                    store.add_season_venue_access(
                        SeasonVenueAccess(id="sva2", season_id="s1",
                                          venue_id="v1", active=False))
                rows = store.season_venue_access_for_season("s1")
                self.assertEqual(len(rows), 2, label)
            finally:
                _teardown(store)


_MIGRATION_VERSION = "029_season_venue_access"


def _drop_migration_029(store):
    """Un-record migration 029 so a re-migrate re-applies its statements over
    whatever venue/season/program data already exists — table/index creation
    is idempotent (IF NOT EXISTS), so only the backfill INSERT actually does
    new work on the second run."""
    cur = store.conn.cursor()
    cur.execute(store.dialect.sql(
        "DELETE FROM schema_migrations WHERE version = ?"), (_MIGRATION_VERSION,))
    if store.backend == "sqlite":
        store.conn.commit()


class MigrationBackfillTest(unittest.TestCase):
    """Migration 029's deterministic backfill from the legacy
    venues.league_id bridge, and its ambiguity-abort guard."""

    def test_unambiguous_program_backfills_one_access_row(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="prog_1", name="P1"))
                store.add_season(
                    Season(id="season_1", program_id="prog_1", name="S1"))
                store.add_venue(
                    Venue(id="venue_1", name="V1", league_id="prog_1"))
                _drop_migration_029(store)
                migrate(store.conn, store.dialect)
                rows = store.all_season_venue_access()
                self.assertEqual(len(rows), 1, label)
                self.assertEqual(rows[0].season_id, "season_1", label)
                self.assertEqual(rows[0].venue_id, "venue_1", label)
                self.assertTrue(rows[0].active, label)
            finally:
                _teardown(store)

    def test_venue_with_no_league_id_is_not_backfilled(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_venue(Venue(id="venue_1", name="V1"))  # league_id=None
                _drop_migration_029(store)
                migrate(store.conn, store.dialect)
                self.assertEqual(store.all_season_venue_access(), [], label)
            finally:
                _teardown(store)

    def test_program_with_no_seasons_is_not_backfilled(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="prog_1", name="P1"))
                store.add_venue(
                    Venue(id="venue_1", name="V1", league_id="prog_1"))
                _drop_migration_029(store)
                migrate(store.conn, store.dialect)
                self.assertEqual(store.all_season_venue_access(), [], label)
            finally:
                _teardown(store)

    def test_ambiguous_program_aborts_the_whole_migration(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                store.add_program(Program(id="prog_1", name="P1"))
                store.add_season(
                    Season(id="season_1", program_id="prog_1", name="S1"))
                store.add_season(
                    Season(id="season_2", program_id="prog_1", name="S2"))
                store.add_venue(
                    Venue(id="venue_1", name="V1", league_id="prog_1"))
                _drop_migration_029(store)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn("venue_1", str(ctx.exception))
                self.assertIn("prog_1", str(ctx.exception))
                # Zero mutation: the table exists (idempotent CREATE ran) but
                # stays empty, and the ledger row was never (re)written.
                self.assertEqual(store.all_season_venue_access(), [], label)
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "SELECT version FROM schema_migrations WHERE version = ?"),
                    (_MIGRATION_VERSION,))
                self.assertIsNone(cur.fetchone(), label)
            finally:
                _teardown(store)

    def test_re_migrate_after_resolving_ambiguity_backfills_correctly(self):
        # An operator resolves the ambiguity (here: by giving one Season a
        # different Program so only one Season remains under prog_1) and
        # re-runs the upgrade — it now succeeds and backfills deterministically.
        store = _fresh(":memory:")
        try:
            store.add_program(Program(id="prog_1", name="P1"))
            store.add_program(Program(id="prog_2", name="P2"))
            store.add_season(Season(id="season_1", program_id="prog_1", name="S1"))
            store.add_season(Season(id="season_2", program_id="prog_1", name="S2"))
            store.add_venue(Venue(id="venue_1", name="V1", league_id="prog_1"))
            _drop_migration_029(store)
            with self.assertRaises(MigrationDataError):
                migrate(store.conn, store.dialect)

            # Resolve: move season_2 to prog_2, so prog_1 now has exactly one.
            season_2 = store.get_season("season_2")
            season_2.program_id = "prog_2"
            store.save_season(season_2)
            migrate(store.conn, store.dialect)
            rows = store.all_season_venue_access()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].season_id, "season_1")
            self.assertEqual(rows[0].venue_id, "venue_1")
        finally:
            _teardown(store)


if __name__ == "__main__":
    unittest.main()
