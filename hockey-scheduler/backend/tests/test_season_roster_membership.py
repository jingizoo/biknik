"""SeasonRosterMembership schema + migration (#205 Slice A).

An athlete's participation is a SEASON-SCOPED membership stint on the
permanent Team + LeagueSeason spine, not a permanent ``player.team_id``.
This module proves the bounded Slice A surface:

  * MODEL + LIFECYCLE — create / status transitions / season-scoped
    attribute edits, each appending an immutable per-membership event plus a
    SetupAuditLog entry; spine coherence (Team's own League's LeagueSeason,
    active registration required); one AUTHORITATIVE active membership per
    (player, Season) with ``affiliate`` as the governed exception; terminal
    rows (released/transferred) as immutable history whose successor stint
    is a NEW row.
  * MIGRATION 052 — deterministic backfill (active players x non-archived
    registered Seasons, values copied, ``srm_legacy_`` compatibility-map
    ids, NULL ``effective_from``, EMPTY event history, GuardianLinks
    untouched), preflight that REPORTS ambiguous/dangling rows and aborts
    with the database left byte-identical (no ledger row, no tables), and
    an idempotent re-run once the operator fixes the named rows.
  * DB ENFORCEMENT — migration 052's two partial unique indexes hold under
    a direct store write that bypasses every service pre-check, and the
    violation is translated to the SAME stable conflict the pre-check
    raises.

THREE STORES. InMemoryStore, SQLite and PostgreSQL. The uniqueness rules are
a dict-scan on one store and real partial-index semantics on the others; the
backfill is real SQL (JOINs over players/teams/registrations/league_seasons/
seasons) that only an actual engine can execute. A SKIP IS NOT A PASS — the
PostgreSQL classes announce loudly when TEST_DATABASE_URL is unset.

FALSIFIERS (each measured while developing this slice):
  * delete the ``WHERE p.is_active = 1`` line of migration 052's INSERT and
    the inactive-player backfill tests turn red on SQLite and PostgreSQL;
  * delete ``AND s.status = 'active'`` and the archived-season tests turn
    red the same way;
  * unregister ``052_season_roster_membership`` from _PRE_MIGRATION_CHECKS
    and the preflight-abort tests turn red (the dirty upgrade "succeeds");
  * drop either partial unique index from the migration and the
    direct-store conflict tests turn red on both SQL engines.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import fresh_sql_store

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    GuardianLink,
    MembershipStatus,
    Position,
    SeasonRosterMembership,
    SeasonStatus,
)
from hockey_scheduler.domain.errors import IntegrityConflictError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.integrity_checks import (
    MigrationDataError,
    assert_season_roster_membership_backfill_ready,
    find_active_players_with_missing_team,
    find_teams_with_duplicate_active_season_registrations,
)
from hockey_scheduler.store.sql_store import migrate

ADMIN = "setup_admin"
_VERSION = "052_season_roster_membership"


def _fixture(api, program="Over 55", season="Fall 2026"):
    """Program -> Season -> Division -> Club -> registered Team, plus the
    LeagueSeason id the registration resolved to. Returns ids."""
    league = api.create_program(program, actor_id=ADMIN)
    sn = api.create_season(league["id"], season, actor_id=ADMIN)
    division = api.create_division(sn["id"], "Div A", actor_id=ADMIN)
    club = api.create_club("Club X", actor_id=ADMIN)
    team = api.create_team(club["id"], division["id"], "Lions", actor_id=ADMIN)
    reg = api.register_team_for_season(sn["id"], team["id"], division["id"],
                                       actor_id=ADMIN)
    ls_id = api.store.get_season_team_registration(reg["id"]).league_season_id
    return league, sn, division, club, team, ls_id


def _player(api, team_id, name="Skater", position="forward", jersey=9,
            shoots="L", is_active=True):
    return api.create_player(team_id, name, position, jersey_number=jersey,
                             shoots=shoots, is_active=is_active,
                             actor_id=ADMIN)


def _archive(store, season_id):
    season = store.get_season(season_id)
    season.status = SeasonStatus.ARCHIVED
    season.archived_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    store.save_season(season)


class MembershipLifecycleTest(unittest.TestCase):
    """Create/status/update lifecycle — Memory + SQLite in every test."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def _each(self):
        for label, store in self._stores():
            api = ApiService(store)
            league, season, division, club, team, ls_id = _fixture(api)
            player = _player(api, team["id"])
            yield label, api, season, team, ls_id, player

    def test_create_copies_player_values_and_stamps_effective_from(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                self.assertNotIn("error", m, label)
                self.assertEqual(m["status"], "active")
                self.assertEqual(m["season_id"], season["id"])
                self.assertEqual(m["position"], "forward")
                self.assertEqual(m["jersey_number"], 9)
                self.assertEqual(m["shoots"], "L")
                self.assertIsNotNone(m["effective_from"])
                self.assertIsNone(m["effective_to"])

    def test_create_with_explicit_overrides_including_none(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], status="applicant",
                    position="defense", jersey_number=None, shoots=None,
                    actor_id=ADMIN)
                self.assertEqual(
                    (m["status"], m["position"], m["jersey_number"],
                     m["shoots"]),
                    ("applicant", "defense", None, None), label)

    def test_create_requires_active_registration_on_teams_own_league(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                # A foreign League's LeagueSeason (same Season) is refused.
                other = api.create_league(season["id"], "Bronze",
                                          actor_id=ADMIN)
                other_ls = api.store.league_season_for(other["id"],
                                                       season["id"])
                res = api.create_season_roster_membership(
                    player["id"], other_ls.id, team["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "membership_league_mismatch", label)
                # An unregistered (deactivated) registration is refused too.
                reg = api.store.registration_for_team_in_league_season(
                    ls_id, team["id"])
                api.unregister_team_from_season(reg.id, actor_id=ADMIN)
                res = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "team_not_registered", label)

    def test_missing_player_team_or_league_season_is_not_found(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                for args in ((("ghost", ls_id, team["id"])),
                             ((player["id"], "ghost", team["id"])),
                             ((player["id"], ls_id, "ghost"))):
                    res = api.create_season_roster_membership(
                        *args, actor_id=ADMIN)
                    self.assertEqual(res["error"]["code"], "not_found",
                                     (label, args))

    def test_terminal_status_cannot_be_created(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                for status in ("released", "transferred"):
                    res = api.create_season_roster_membership(
                        player["id"], ls_id, team["id"], status=status,
                        actor_id=ADMIN)
                    self.assertEqual(
                        res["error"]["details"]["reason"],
                        "membership_status_terminal_create", (label, status))

    def test_one_open_stint_per_player_per_league_season(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                first = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                dup = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], status="applicant",
                    actor_id=ADMIN)
                self.assertEqual(dup["error"]["details"]["reason"],
                                 "membership_open_conflict", label)
                self.assertEqual(
                    dup["error"]["details"]["affected_membership_ids"],
                    [first["id"]], label)

    def test_one_authoritative_active_membership_per_player_per_season(self):
        # Cross-League, same Season: the second ACTIVE membership is refused;
        # AFFILIATE — the epic's governed call-up exception — is allowed.
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                program_id = api.store.get_season(season["id"]).program_id
                league_b = api.create_league(season["id"], "Bronze",
                                             actor_id=ADMIN)
                club_b = api.create_club("Club B", actor_id=ADMIN)
                team_b = api.create_team(
                    club_b["id"], name="Bears", program_id=program_id,
                    league_id=league_b["id"], actor_id=ADMIN)
                reg_b = api.register_team_for_season(
                    season["id"], team_b["id"], league_id=league_b["id"],
                    actor_id=ADMIN)
                self.assertNotIn("error", reg_b, label)
                ls_b = api.store.get_season_team_registration(
                    reg_b["id"]).league_season_id
                clash = api.create_season_roster_membership(
                    player["id"], ls_b, team_b["id"], jersey_number=None,
                    actor_id=ADMIN)
                self.assertEqual(clash["error"]["details"]["reason"],
                                 "membership_active_conflict", label)
                affiliate = api.create_season_roster_membership(
                    player["id"], ls_b, team_b["id"], status="affiliate",
                    jersey_number=None, actor_id=ADMIN)
                self.assertNotIn("error", affiliate, label)
                self.assertEqual(affiliate["status"], "affiliate", label)

    def test_status_lifecycle_terminal_immutability_and_new_stint(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], status="applicant",
                    actor_id=ADMIN)
                self.assertIsNone(m["effective_to"], label)
                act = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(act["status"], "active", label)
                noop = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(noop["error"]["details"]["reason"],
                                 "membership_status_unchanged", label)
                rel = api.set_season_roster_membership_status(
                    m["id"], "released", reason="cut", actor_id=ADMIN)
                self.assertEqual(rel["status"], "released", label)
                self.assertIsNotNone(rel["effective_to"], label)
                frozen = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(frozen["error"]["details"]["reason"],
                                 "membership_terminal", label)
                frozen_edit = api.update_season_roster_membership(
                    m["id"], jersey_number=42)
                self.assertEqual(frozen_edit["error"]["details"]["reason"],
                                 "membership_terminal", label)
                # The ended stint is history; the next one is a NEW row.
                m2 = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                self.assertNotIn("error", m2, label)
                self.assertNotEqual(m2["id"], m["id"], label)

    def test_reactivation_conflicts_with_another_active_membership(self):
        # Parked (inactive) reactivates freely; but once ANOTHER row holds
        # the authoritative slot this Season — here an affiliate row on a
        # second League promoted to active — reactivation is refused.
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m1 = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                api.set_season_roster_membership_status(
                    m1["id"], "inactive", actor_id=ADMIN)
                m1b = api.set_season_roster_membership_status(
                    m1["id"], "active", actor_id=ADMIN)  # reactivate: fine
                self.assertEqual(m1b["status"], "active", label)
                api.set_season_roster_membership_status(
                    m1["id"], "inactive", actor_id=ADMIN)  # park again
                program_id = api.store.get_season(season["id"]).program_id
                league_b = api.create_league(season["id"], "Bronze",
                                             actor_id=ADMIN)
                club_b = api.create_club("Club B", actor_id=ADMIN)
                team_b = api.create_team(
                    club_b["id"], name="Bears", program_id=program_id,
                    league_id=league_b["id"], actor_id=ADMIN)
                api.register_team_for_season(
                    season["id"], team_b["id"], league_id=league_b["id"],
                    actor_id=ADMIN)
                ls_b = api.store.league_season_for(
                    league_b["id"], season["id"]).id
                m2 = api.create_season_roster_membership(
                    player["id"], ls_b, team_b["id"], status="affiliate",
                    jersey_number=None, actor_id=ADMIN)
                promoted = api.set_season_roster_membership_status(
                    m2["id"], "active", actor_id=ADMIN)  # no other active
                self.assertEqual(promoted["status"], "active", label)
                clash = api.set_season_roster_membership_status(
                    m1["id"], "active", actor_id=ADMIN)
                self.assertEqual(clash["error"]["details"]["reason"],
                                 "membership_active_conflict", label)

    def test_season_scoped_jersey_uniqueness_and_release_frees_number(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m1 = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)  # #9
                p2 = _player(api, team["id"], name="Second",
                             position="defense", jersey=4)
                conflict = api.create_season_roster_membership(
                    p2["id"], ls_id, team["id"], jersey_number=9,
                    actor_id=ADMIN)
                self.assertEqual(conflict["error"]["code"], "conflict", label)
                self.assertEqual(
                    conflict["error"]["details"]["reason"],
                    "duplicate_membership_jersey_number", label)
                api.set_season_roster_membership_status(
                    m1["id"], "released", actor_id=ADMIN)
                freed = api.create_season_roster_membership(
                    p2["id"], ls_id, team["id"], jersey_number=9,
                    actor_id=ADMIN)
                self.assertNotIn("error", freed, label)

    def test_attribute_update_writes_events_and_audit_noop_writes_nothing(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                up = api.update_season_roster_membership(
                    m["id"], jersey_number=12, position="defense",
                    shoots="R", reason="correction", actor_id=ADMIN)
                self.assertEqual(
                    (up["jersey_number"], up["position"], up["shoots"]),
                    (12, "defense", "R"), label)
                events = api.list_season_roster_membership_events(
                    m["id"])["events"]
                self.assertEqual([e["action"] for e in events],
                                 ["created", "attributes_changed"], label)
                changed = events[-1]["detail"]
                self.assertEqual(changed["jersey_number"],
                                 {"from": 9, "to": 12}, label)
                self.assertEqual(changed["position"],
                                 {"from": "forward", "to": "defense"}, label)
                self.assertEqual(events[-1]["reason"], "correction", label)
                self.assertEqual(events[-1]["actor_id"], ADMIN, label)
                # Genuine no-op: nothing written, the history never lies.
                api.update_season_roster_membership(m["id"], jersey_number=12)
                self.assertEqual(
                    len(api.list_season_roster_membership_events(
                        m["id"])["events"]), 2, label)
                audit = [a for a in api.store.all_setup_audit()
                         if a.entity_id == m["id"]]
                self.assertEqual(
                    [a.action for a in audit],
                    ["season_roster_membership_created",
                     "season_roster_membership_updated"], label)
                self.assertTrue(all(a.actor_id == ADMIN for a in audit),
                                label)
                self.assertEqual(audit[-1].detail["changed_fields"],
                                 ["jersey_number", "position", "shoots"],
                                 label)

    def test_validation_rejects_bad_status_jersey_shoots_position(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                bad_status = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], status="benched",
                    actor_id=ADMIN)
                self.assertEqual(bad_status["error"]["details"]["reason"],
                                 "invalid_membership_status", label)
                bad_jersey = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], jersey_number=99,
                    actor_id=ADMIN)
                self.assertEqual(bad_jersey["error"]["details"]["reason"],
                                 "invalid_jersey_number", label)
                bad_shoots = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], shoots="both",
                    actor_id=ADMIN)
                self.assertEqual(bad_shoots["error"]["details"]["reason"],
                                 "invalid_shoots", label)
                bad_pos = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], position="rover",
                    actor_id=ADMIN)
                self.assertEqual(bad_pos["error"]["details"]["reason"],
                                 "invalid_position", label)

    def test_archived_season_is_read_only_for_memberships(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                _archive(api.store, season["id"])
                for res in (
                        api.create_season_roster_membership(
                            player["id"], ls_id, team["id"],
                            status="applicant", actor_id=ADMIN),
                        api.set_season_roster_membership_status(
                            m["id"], "inactive", actor_id=ADMIN),
                        api.update_season_roster_membership(
                            m["id"], jersey_number=13)):
                    self.assertIn("error", res, label)
                # Reads still work on archived history.
                self.assertEqual(
                    api.get_season_roster_membership(m["id"])["id"],
                    m["id"], label)

    def test_list_filters_and_filter_required(self):
        for label, api, season, team, ls_id, player in self._each():
            with self.subTest(backend=label):
                m = api.create_season_roster_membership(
                    player["id"], ls_id, team["id"], actor_id=ADMIN)
                by_season = api.list_season_roster_memberships(
                    season_id=season["id"])["memberships"]
                by_pair = api.list_season_roster_memberships(
                    league_season_id=ls_id,
                    team_id=team["id"])["memberships"]
                by_player = api.list_season_roster_memberships(
                    player_id=player["id"])["memberships"]
                for rows in (by_season, by_pair, by_player):
                    self.assertEqual([r["id"] for r in rows], [m["id"]],
                                     label)
                none = api.list_season_roster_memberships()
                self.assertEqual(none["error"]["details"]["reason"],
                                 "membership_filter_required", label)


class MembershipPersistenceTest(unittest.TestCase):
    """SQLite file round-trip: memberships + events survive a reopen with
    types (enums/datetimes) intact — the restart proof."""

    def _durable(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        return path

    def test_membership_and_events_survive_reopen(self):
        path = self._durable()
        api = ApiService(SqlStore(path))
        league, season, division, club, team, ls_id = _fixture(api)
        player = _player(api, team["id"], position="goalie", jersey=31)
        m = api.create_season_roster_membership(
            player["id"], ls_id, team["id"], actor_id=ADMIN)
        api.set_season_roster_membership_status(
            m["id"], "injured", reason="lower body", actor_id=ADMIN)
        del api

        reopened = SqlStore(path)
        row = reopened.get_season_roster_membership(m["id"])
        self.assertIs(row.status, MembershipStatus.INJURED)
        self.assertIs(row.position, Position.GOALIE)
        self.assertEqual(row.jersey_number, 31)
        self.assertEqual(row.season_id, season["id"])
        self.assertIsInstance(row.effective_from, datetime)
        events = reopened.events_for_membership(m["id"])
        self.assertEqual([e.action for e in events],
                         ["created", "status_changed"])
        self.assertEqual(events[-1].reason, "lower body")
        self.assertIsInstance(events[-1].at, datetime)
        self.assertEqual(events[-1].detail,
                         {"from": "active", "to": "injured"})


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _downgrade_052(store):
    """Simulate a pre-052 database: drop the two tables (their indexes go
    with them) and un-record the version, so the next migrate() re-runs 052
    against whatever data the test has planted."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS season_roster_membership_events")
        cur.execute("DROP TABLE IF EXISTS season_roster_memberships")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _seed_legacy(api):
    """A pre-052-shaped world: one active Season with a registered Team, an
    active + an inactive Player, an ARCHIVED Season the same Team is also
    registered in, and a GuardianLink on the active Player. Returns the ids
    the backfill assertions need."""
    league, season, division, club, team, ls_id = _fixture(api)
    active = _player(api, team["id"], name="Act", jersey=9)
    inactive = _player(api, team["id"], name="Ina", position="defense",
                       jersey=2, is_active=False)
    archived = api.create_season(league["id"], "Old Season", actor_id=ADMIN)
    reg2 = api.register_team_for_season(archived["id"], team["id"],
                                        actor_id=ADMIN)
    ls_archived = api.store.get_season_team_registration(
        reg2["id"]).league_season_id
    _archive(api.store, archived["id"])
    api.store.add_guardian_link(GuardianLink(
        id="gl_1", guardian_user_id="guardian_1", player_id=active["id"],
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc), verified=True))
    return {"season": season["id"], "ls": ls_id, "team": team["id"],
            "active": active["id"], "inactive": inactive["id"],
            "archived_season": archived["id"], "ls_archived": ls_archived}


class MembershipBackfillTest(unittest.TestCase):
    """Migration 052's deterministic backfill + preflight + rollback, on
    SQLite AND (when configured) PostgreSQL via _sql_backends()."""

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def test_backfill_is_deterministic_and_preserves_guardian_links(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                _downgrade_052(store)
                migrate(store.conn, store.dialect)  # re-applies 052

                rows = store.all_season_roster_memberships()
                # EXACTLY one membership: the active player in the active
                # Season. No row for the inactive player; none in the
                # archived Season despite its active registration.
                self.assertEqual(len(rows), 1, (label, rows))
                m = rows[0]
                self.assertEqual(
                    m.id, f"srm_legacy_{ids['active']}_{ids['ls']}", label)
                self.assertEqual(m.player_id, ids["active"], label)
                self.assertEqual(m.season_id, ids["season"], label)
                self.assertEqual(m.team_id, ids["team"], label)
                self.assertIs(m.status, MembershipStatus.ACTIVE, label)
                self.assertIs(m.position, Position.FORWARD, label)
                self.assertEqual(m.jersey_number, 9, label)
                self.assertEqual(m.shoots, "L", label)
                # No fabricated dates, no fabricated history.
                self.assertIsNone(m.effective_from, label)
                self.assertIsNone(m.effective_to, label)
                self.assertEqual(store.events_for_membership(m.id), [], label)
                # GuardianLink untouched and still resolving to the same
                # preserved Player id.
                link = store.get_guardian_link("gl_1")
                self.assertEqual(link.player_id, ids["active"], label)
                self.assertTrue(link.verified, label)
                # Ledger recorded; a second migrate() applies nothing new.
                migrate(store.conn, store.dialect)
                self.assertEqual(
                    len(store.all_season_roster_memberships()), 1, label)
            finally:
                self._cleanup(store)

    def test_preflight_reports_dangling_player_and_leaves_db_unchanged(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                cur = store.conn.cursor()
                if store.backend == "sqlite":
                    cur.execute("PRAGMA foreign_keys = OFF")
                else:
                    cur.execute("ALTER TABLE players "
                                "DROP CONSTRAINT IF EXISTS fk_players_team")
                cur.execute(store.dialect.sql(
                    "UPDATE players SET team_id = ? WHERE id = ?"),
                    ("ghost", ids["active"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_052(store)

                self.assertEqual(
                    find_active_players_with_missing_team(store.conn),
                    [(ids["active"], "ghost")], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                # ROLLBACK PROOF: the abort happened before any DDL — no
                # ledger row, no membership tables.
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "SELECT COUNT(*) AS n FROM schema_migrations "
                    "WHERE version = ?"), (_VERSION,))
                self.assertEqual(cur.fetchone()["n"], 0, label)
                if store.backend == "sqlite":
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM sqlite_master WHERE "
                        "type='table' AND name='season_roster_memberships'")
                else:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM information_schema.tables "
                        "WHERE table_name='season_roster_memberships'")
                self.assertEqual(cur.fetchone()["n"], 0, label)
                # Operator fixes the named row; the re-run applies cleanly.
                cur.execute(store.dialect.sql(
                    "UPDATE players SET team_id = ? WHERE id = ?"),
                    (ids["team"], ids["active"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                self.assertEqual(
                    len(store.all_season_roster_memberships()), 1, label)
            finally:
                self._cleanup(store)

    def test_preflight_reports_duplicate_active_registrations(self):
        from hockey_scheduler.domain import SeasonTeamRegistration
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                # Legacy-shaped corruption: a SECOND active registration for
                # the same Team resolving to the same Season, via another
                # League's LeagueSeason (bypasses the service, which forbids
                # creating this state today).
                program_id = api.store.get_season(ids["season"]).program_id
                league_b = api.create_league(ids["season"], "Bronze",
                                             actor_id=ADMIN)
                club_b = api.create_club("Club B", actor_id=ADMIN)
                team_b = api.create_team(
                    club_b["id"], name="Bears", program_id=program_id,
                    league_id=league_b["id"], actor_id=ADMIN)
                reg_b = api.register_team_for_season(
                    ids["season"], team_b["id"], league_id=league_b["id"],
                    actor_id=ADMIN)
                ls_b = api.store.get_season_team_registration(
                    reg_b["id"]).league_season_id
                with store.transaction():
                    store.add_season_team_registration(SeasonTeamRegistration(
                        id="streg_dup", league_season_id=ls_b,
                        team_id=ids["team"], active=True))
                _downgrade_052(store)

                self.assertEqual(
                    find_teams_with_duplicate_active_season_registrations(
                        store.conn),
                    [(ids["team"], ids["season"])], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    assert_season_roster_membership_backfill_ready(store.conn)
                self.assertIn(ids["team"], str(ctx.exception), label)
                with self.assertRaises(MigrationDataError, msg=label):
                    migrate(store.conn, store.dialect)
                # Deactivate the duplicate; the upgrade proceeds and derives
                # the membership from the surviving registration only.
                with store.transaction():
                    dup = store.get_season_team_registration("streg_dup")
                    dup.active = False
                    store.save_season_team_registration(dup)
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows
                     if m.team_id == ids["team"]],
                    [(ids["active"], ids["ls"])], label)
            finally:
                self._cleanup(store)


class MembershipIndexEnforcementTest(unittest.TestCase):
    """Migration 052's partial unique indexes hold when the service layer is
    bypassed entirely, and the violation carries the SAME stable conflict
    the pre-checks raise — on SQLite AND (when configured) PostgreSQL."""

    def _seeded(self, url):
        store = _fresh(url)
        api = ApiService(store)
        league, season, division, club, team, ls_id = _fixture(api)
        player = _player(api, team["id"])
        m = api.create_season_roster_membership(
            player["id"], ls_id, team["id"], actor_id=ADMIN)
        return store, api, season, team, ls_id, player, m

    def test_second_active_membership_same_player_season_is_conflict(self):
        for label, url in _sql_backends():
            store, api, season, team, ls_id, player, m = self._seeded(url)
            try:
                with self.assertRaises(IntegrityConflictError,
                                       msg=label) as ctx:
                    with store.transaction():
                        store.add_season_roster_membership(
                            SeasonRosterMembership(
                                id="srm_dup", player_id=player["id"],
                                league_season_id=ls_id,
                                season_id=season["id"], team_id=team["id"],
                                status=MembershipStatus.ACTIVE,
                                position=Position.FORWARD,
                                jersey_number=None))
                self.assertEqual(ctx.exception.details["reason"],
                                 "membership_active_conflict", label)
                # Zero writes: the loser's row is not there.
                self.assertIsNone(
                    store.get_season_roster_membership("srm_dup"), label)
            finally:
                self._cleanup(store)

    def test_second_active_jersey_same_league_season_team_is_conflict(self):
        for label, url in _sql_backends():
            store, api, season, team, ls_id, player, m = self._seeded(url)
            try:
                p2 = _player(api, team["id"], name="Second",
                             position="defense", jersey=4)
                with self.assertRaises(IntegrityConflictError,
                                       msg=label) as ctx:
                    with store.transaction():
                        store.add_season_roster_membership(
                            SeasonRosterMembership(
                                id="srm_j", player_id=p2["id"],
                                league_season_id=ls_id,
                                season_id=season["id"], team_id=team["id"],
                                status=MembershipStatus.ACTIVE,
                                position=Position.DEFENSE,
                                jersey_number=9))
                self.assertEqual(ctx.exception.details["reason"],
                                 "duplicate_membership_jersey_number", label)
            finally:
                self._cleanup(store)

    def test_non_active_rows_are_outside_both_indexes(self):
        for label, url in _sql_backends():
            store, api, season, team, ls_id, player, m = self._seeded(url)
            try:
                with store.transaction():
                    for i, status in enumerate(
                            (MembershipStatus.RELEASED,
                             MembershipStatus.TRANSFERRED,
                             MembershipStatus.AFFILIATE)):
                        store.add_season_roster_membership(
                            SeasonRosterMembership(
                                id=f"srm_h{i}", player_id=player["id"],
                                league_season_id=ls_id,
                                season_id=season["id"], team_id=team["id"],
                                status=status, position=Position.FORWARD,
                                jersey_number=9))
                self.assertEqual(
                    len(store.all_season_roster_memberships()), 4, label)
            finally:
                self._cleanup(store)

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #205 Slice A membership lifecycle was NOT "
            "exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: the "
            "authoritative-active and season-Team jersey rules are partial "
            "unique indexes whose predicate/NULL semantics only a real "
            "engine evaluates, and the 052 backfill is multi-table SQL that "
            "must run under PostgreSQL's stricter typing. Set "
            "TEST_DATABASE_URL (run_parallel.py --postgres does) to run it.")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class MembershipPostgresLifecycleTest(unittest.TestCase):
    """The full service lifecycle against real PostgreSQL — the tri-store
    proof's third leg (Memory/SQLite legs run in MembershipLifecycleTest).
    Backfill/preflight/index enforcement additionally run on PostgreSQL via
    _sql_backends() in the classes above."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        self.api = ApiService(self.store)
        (self.league, self.season, self.division, self.club, self.team,
         self.ls_id) = _fixture(self.api)
        self.player = _player(self.api, self.team["id"])

    def test_lifecycle_round_trips_on_postgres(self):
        api = self.api
        m = api.create_season_roster_membership(
            self.player["id"], self.ls_id, self.team["id"], actor_id=ADMIN)
        self.assertNotIn("error", m)
        dup = api.create_season_roster_membership(
            self.player["id"], self.ls_id, self.team["id"], actor_id=ADMIN)
        self.assertEqual(dup["error"]["details"]["reason"],
                         "membership_open_conflict")
        p2 = _player(api, self.team["id"], name="Second", position="defense",
                     jersey=4)
        jconf = api.create_season_roster_membership(
            p2["id"], self.ls_id, self.team["id"], jersey_number=9,
            actor_id=ADMIN)
        self.assertEqual(jconf["error"]["details"]["reason"],
                         "duplicate_membership_jersey_number")
        upd = api.update_season_roster_membership(
            m["id"], jersey_number=12, reason="switch", actor_id=ADMIN)
        self.assertEqual(upd["jersey_number"], 12)
        rel = api.set_season_roster_membership_status(
            m["id"], "released", reason="cut", actor_id=ADMIN)
        self.assertEqual(rel["status"], "released")
        self.assertIsNotNone(rel["effective_to"])
        frozen = api.set_season_roster_membership_status(
            m["id"], "active", actor_id=ADMIN)
        self.assertEqual(frozen["error"]["details"]["reason"],
                         "membership_terminal")
        events = api.list_season_roster_membership_events(m["id"])["events"]
        self.assertEqual([e["action"] for e in events],
                         ["created", "attributes_changed", "status_changed"])
        # Durable across a second connection to the same database.
        second = SqlStore(os.environ["TEST_DATABASE_URL"])
        try:
            row = second.get_season_roster_membership(m["id"])
            self.assertIs(row.status, MembershipStatus.RELEASED)
            self.assertEqual(row.jersey_number, 12)
            self.assertEqual(len(second.events_for_membership(m["id"])), 3)
        finally:
            second.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
