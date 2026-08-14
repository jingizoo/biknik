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
import threading
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
    find_active_players_with_dangling_league_season_parents,
    find_active_players_with_dangling_registration_target,
    find_active_players_with_missing_team,
    find_active_players_with_program_mismatch,
    find_active_players_with_team_league_mismatch,
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
                    m1["id"], "released", reason="cut", actor_id=ADMIN)
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


class MembershipTerminalTransitionFloorTest(unittest.TestCase):
    """#205 review round 1 finding 5 — the PR narrows back to Slice A scope:
    the schema stays capable of representing released/transferred (the enum
    values are untouched), but the facade/service floor now REFUSES to set
    either without a non-blank actor_id AND reason, with zero mutation.

    This is deliberately a FLOOR, not the authorized transfer/release/
    deadline-policy/override workflow #212 places in a later #205 slice —
    see create_season_roster_membership's and set_season_roster_membership_
    status's docstrings and the PR body for the explicit scope statement."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def _each(self):
        for label, store in self._stores():
            api = ApiService(store)
            league, season, division, club, team, ls_id = _fixture(api)
            player = _player(api, team["id"])
            m = api.create_season_roster_membership(
                player["id"], ls_id, team["id"], actor_id=ADMIN)
            yield label, api, m

    def _snapshot(self, api, membership_id):
        row = api.store.get_season_roster_membership(membership_id)
        events = api.store.events_for_membership(membership_id)
        audits = [a for a in api.store.all_setup_audit()
                 if a.entity_id == membership_id]
        return row.status, len(events), len(audits)

    def test_no_actor_refuses_terminal_transition_zero_write(self):
        for label, api, m in self._each():
            with self.subTest(backend=label):
                before = self._snapshot(api, m["id"])
                for kwargs in ({"reason": "cut"},                # no actor
                              {"reason": "cut", "actor_id": ""},  # blank
                              {"reason": "cut", "actor_id": "   "}):
                    res = api.set_season_roster_membership_status(
                        m["id"], "released", **kwargs)
                    self.assertEqual(
                        res["error"]["details"]["reason"],
                        "terminal_transition_requires_actor",
                        (label, kwargs, res))
                    self.assertEqual(self._snapshot(api, m["id"]), before,
                                     (label, kwargs))

    def test_no_reason_refuses_terminal_transition_zero_write(self):
        for label, api, m in self._each():
            with self.subTest(backend=label):
                before = self._snapshot(api, m["id"])
                for kwargs in ({"actor_id": ADMIN},                # no reason
                              {"actor_id": ADMIN, "reason": ""},    # blank
                              {"actor_id": ADMIN, "reason": "   "}):
                    res = api.set_season_roster_membership_status(
                        m["id"], "released", **kwargs)
                    self.assertEqual(
                        res["error"]["details"]["reason"],
                        "terminal_transition_requires_reason",
                        (label, kwargs, res))
                    self.assertEqual(self._snapshot(api, m["id"]), before,
                                     (label, kwargs))

    def test_non_terminal_transition_needs_neither(self):
        # The floor is scoped to TERMINAL targets only — parking a
        # membership (a fully reversible, non-terminal transition) is
        # unaffected, matching the review's own "at minimum" floor framing.
        for label, api, m in self._each():
            with self.subTest(backend=label):
                res = api.set_season_roster_membership_status(
                    m["id"], "inactive")
                self.assertNotIn("error", res, (label, res))

    def test_authorized_terminal_transition_succeeds(self):
        for label, api, m in self._each():
            with self.subTest(backend=label):
                res = api.set_season_roster_membership_status(
                    m["id"], "released", reason="cut", actor_id=ADMIN)
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["status"], "released", label)
                events = api.list_season_roster_membership_events(
                    m["id"])["events"]
                self.assertEqual(events[-1]["reason"], "cut", label)
                self.assertEqual(events[-1]["actor_id"], ADMIN, label)


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


class MembershipBackfillSpineTest(unittest.TestCase):
    """Migration 052's preflight validates the FULL registration ->
    LeagueSeason -> Season/League spine, and Team<->LeagueSeason League (and
    Program) coherence, before any DDL (#205 review round 1 finding 3).

    None of these four corruption shapes touch a column any FK constraint
    actually covers (``season_team_registrations.league_season_id``,
    ``league_seasons.season_id``/``league_id`` and ``teams.league_id`` were
    all added by plain ``ALTER TABLE ... ADD COLUMN`` — no
    ``FOREIGN KEY``), so no FK-disable dance is needed to plant them; that
    is exactly WHY they can reach an ordinary UPDATE undetected in the first
    place, and exactly why this preflight exists.
    """

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def _assert_clean_rollback(self, store, label):
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = ?"),
            (_VERSION,))
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

    def test_dangling_registration_target_aborts_and_repairs(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                reg = api.store.registration_for_team_in_league_season(
                    ids["ls"], ids["team"])
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE season_team_registrations "
                    "SET league_season_id = ? WHERE id = ?"),
                    ("ghost_ls", reg.id))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_052(store)

                self.assertEqual(
                    find_active_players_with_dangling_registration_target(
                        store.conn),
                    [(ids["active"], reg.id, "ghost_ls")], label)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_clean_rollback(store, label)
                # Repair: point the registration back; the re-run applies
                # cleanly and derives the exact same membership as before.
                cur.execute(store.dialect.sql(
                    "UPDATE season_team_registrations "
                    "SET league_season_id = ? WHERE id = ?"),
                    (ids["ls"], reg.id))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows],
                    [(ids["active"], ids["ls"])], label)
            finally:
                self._cleanup(store)

    def test_dangling_league_season_parent_aborts_and_repairs(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                original_league_id = api.store.get_league_season(
                    ids["ls"]).league_id
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE league_seasons SET league_id = ? WHERE id = ?"),
                    ("ghost_league", ids["ls"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_052(store)

                found = find_active_players_with_dangling_league_season_parents(
                    store.conn)
                self.assertEqual(len(found), 1, (label, found))
                self.assertEqual(found[0][0], ids["active"], (label, found))
                self.assertEqual(found[0][3], "ghost_league", (label, found))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_clean_rollback(store, label)
                # Repair: restore the LeagueSeason's original League.
                cur.execute(store.dialect.sql(
                    "UPDATE league_seasons SET league_id = ? WHERE id = ?"),
                    (original_league_id, ids["ls"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows],
                    [(ids["active"], ids["ls"])], label)
            finally:
                self._cleanup(store)

    def test_team_league_mismatch_aborts_and_repairs(self):
        # Mirrors the review's exact scenario: a registration corrupted to
        # point at ANOTHER League's LeagueSeason in the SAME active Season.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                league_b = api.create_league(ids["season"], "Bronze",
                                             actor_id=ADMIN)
                ls_b = api.store.league_season_for(league_b["id"],
                                                    ids["season"])
                reg = api.store.registration_for_team_in_league_season(
                    ids["ls"], ids["team"])
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE season_team_registrations "
                    "SET league_season_id = ? WHERE id = ?"),
                    (ls_b.id, reg.id))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_052(store)

                found = find_active_players_with_team_league_mismatch(
                    store.conn)
                self.assertEqual(len(found), 1, (label, found))
                self.assertEqual(found[0][0], ids["active"], (label, found))
                self.assertEqual(found[0][3], ls_b.id, (label, found))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_clean_rollback(store, label)
                # Repair: point the registration back at the Team's OWN League.
                cur.execute(store.dialect.sql(
                    "UPDATE season_team_registrations "
                    "SET league_season_id = ? WHERE id = ?"),
                    (ids["ls"], reg.id))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows],
                    [(ids["active"], ids["ls"])], label)
            finally:
                self._cleanup(store)

    def test_program_mismatch_aborts_and_repairs(self):
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                other_program = api.create_program("Other Program",
                                                    actor_id=ADMIN)
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE leagues SET program_id = ? WHERE id = (SELECT "
                    "league_id FROM league_seasons WHERE id = ?)"),
                    (other_program["id"], ids["ls"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_052(store)

                found = find_active_players_with_program_mismatch(store.conn)
                self.assertEqual(len(found), 1, (label, found))
                self.assertEqual(found[0][0], ids["active"], (label, found))
                self.assertEqual(found[0][3], other_program["id"],
                                 (label, found))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_clean_rollback(store, label)
                # Repair: restore the League's original Program.
                original_program_id = api.store.get_season(
                    ids["season"]).program_id
                cur.execute(store.dialect.sql(
                    "UPDATE leagues SET program_id = ? WHERE id = (SELECT "
                    "league_id FROM league_seasons WHERE id = ?)"),
                    (original_program_id, ids["ls"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows],
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
        return store, api, season, team, ls_id, player, m, division

    def _second_league_season(self, api, season):
        """A second League (own LeagueSeason, own registered Team) in the
        SAME Season — isolates the SEASON-scoped active-per-player index
        from the LEAGUE-SEASON-scoped open-stint index (#205 review round 1
        finding 1): a row on THIS league_season can violate one without
        the other."""
        program_id = api.store.get_season(season["id"]).program_id
        league_b = api.create_league(season["id"], "Bronze", actor_id=ADMIN)
        club_b = api.create_club("Club B", actor_id=ADMIN)
        team_b = api.create_team(club_b["id"], name="Bears",
                                 program_id=program_id,
                                 league_id=league_b["id"], actor_id=ADMIN)
        reg_b = api.register_team_for_season(
            season["id"], team_b["id"], league_id=league_b["id"],
            actor_id=ADMIN)
        ls_b = api.store.get_season_team_registration(
            reg_b["id"]).league_season_id
        return team_b, ls_b

    def test_second_active_membership_same_player_season_is_conflict(self):
        # #205 review round 1 finding 1 — the duplicate lands on a DIFFERENT
        # LeagueSeason (own League) so ONLY the SEASON-scoped active index is
        # violated, isolating it from the new LeagueSeason-scoped open-stint
        # index below (both would otherwise fire on the same row, and which
        # one an engine reports first is not something either store's
        # contract promises).
        for label, url in _sql_backends():
            store, api, season, team, ls_id, player, m, division = self._seeded(url)
            try:
                team_b, ls_b = self._second_league_season(api, season)
                with self.assertRaises(IntegrityConflictError,
                                       msg=label) as ctx:
                    with store.transaction():
                        store.add_season_roster_membership(
                            SeasonRosterMembership(
                                id="srm_dup", player_id=player["id"],
                                league_season_id=ls_b,
                                season_id=season["id"], team_id=team_b["id"],
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
            store, api, season, team, ls_id, player, m, division = self._seeded(url)
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

    def test_terminal_rows_are_outside_all_three_indexes(self):
        # RELEASED/TRANSFERRED are history and never collide with anything —
        # not the active-per-season or active-jersey rules (pre-existing),
        # and not the new open-stint-per-league_season rule either (#205
        # review round 1 finding 1): terminal statuses are its explicit
        # exclusion (``status NOT IN ('released', 'transferred')``).
        for label, url in _sql_backends():
            store, api, season, team, ls_id, player, m, division = self._seeded(url)
            try:
                with store.transaction():
                    for i, status in enumerate(
                            (MembershipStatus.RELEASED,
                             MembershipStatus.TRANSFERRED)):
                        store.add_season_roster_membership(
                            SeasonRosterMembership(
                                id=f"srm_h{i}", player_id=player["id"],
                                league_season_id=ls_id,
                                season_id=season["id"], team_id=team["id"],
                                status=status, position=Position.FORWARD,
                                jersey_number=9))
                self.assertEqual(
                    len(store.all_season_roster_memberships()), 3, label)
            finally:
                self._cleanup(store)

    def test_open_stint_index_rejects_second_non_terminal_same_league_season(
            self):
        # #205 review round 1 finding 1 — direct-store bypass (no service
        # lock at all): applicant/affiliate/injured on a DIFFERENT Team of
        # the SAME LeagueSeason as the already-open `m` (active) must all be
        # rejected by ``ux_srm_open_player_league_season``, translated to
        # the SAME ``membership_open_conflict`` reason the service pre-check
        # raises. 'affiliate' is the epic's call-up exception to the
        # ACTIVE-per-Season rule ONLY — not to this one (a call-up is still
        # exactly one stint on its OWN LeagueSeason).
        for label, url in _sql_backends():
            for status in (MembershipStatus.APPLICANT,
                          MembershipStatus.AFFILIATE,
                          MembershipStatus.INJURED):
                store, api, season, team, ls_id, player, m, division = self._seeded(url)
                try:
                    # Same club/division as the seeded team, so it resolves
                    # to the SAME LeagueSeason (``ls_id``) — mirrors
                    # ``_fixture``'s own team-creation + registration calls.
                    other_team = api.create_team(
                        team["club_id"], division["id"], "Other",
                        actor_id=ADMIN)
                    api.register_team_for_season(
                        season["id"], other_team["id"], division["id"],
                        actor_id=ADMIN)
                    sub_label = f"{label}/{status.value}"
                    with self.assertRaises(IntegrityConflictError,
                                           msg=sub_label) as ctx:
                        with store.transaction():
                            store.add_season_roster_membership(
                                SeasonRosterMembership(
                                    id=f"srm_open_{status.value}",
                                    player_id=player["id"],
                                    league_season_id=ls_id,
                                    season_id=season["id"],
                                    team_id=other_team["id"],
                                    status=status, position=Position.FORWARD,
                                    jersey_number=None))
                    self.assertEqual(ctx.exception.details["reason"],
                                     "membership_open_conflict", sub_label)
                    self.assertEqual(ctx.exception.details["player_id"],
                                     player["id"], sub_label)
                    self.assertEqual(ctx.exception.details["league_season_id"],
                                     ls_id, sub_label)
                    self.assertIsNone(
                        store.get_season_roster_membership(
                            f"srm_open_{status.value}"), sub_label)
                finally:
                    self._cleanup(store)

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()


class MembershipEventOrderingTest(unittest.TestCase):
    """Real monotonic ``seq`` ordering (#205 review round 1 finding 4).

    An injected constant clock gives every event on one membership the
    IDENTICAL ``at`` timestamp — reproducing a fast operator or a test's
    shared clock — so ``at`` cannot be what orders history. Past 9 events,
    ``id`` (TEXT) can't either: SQLite/PostgreSQL's ``ORDER BY id`` sorts
    ``srme_10`` before ``srme_2`` lexically, while InMemoryStore's dict
    preserves real insertion order — a silent Memory/SQL divergence. Only
    ``seq`` (a real INTEGER) is compared numerically by every engine.

    Falsifier: `git stash` the seq column/``next_seq``/``ORDER BY seq``
    changes (keep the domain field) and this turns red on SQLite/PostgreSQL
    specifically past the 10th event, while Memory stays accidentally green
    (dict insertion order) — exactly the silent tri-store divergence the
    review named.
    """

    FIXED = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    N_EVENTS = 12  # "created" + 11 "attributes_changed" >= the review's 11+

    def _generate(self, store):
        """Create ``N_EVENTS`` history rows on ONE membership, all at the
        SAME injected timestamp, and return (membership_id,
        expected_jersey_sequence)."""
        api = ApiService(store)
        api.setup.clock = lambda: self.FIXED
        league, season, division, club, team, ls_id = _fixture(api)
        player = _player(api, team["id"], jersey=9)
        m = api.create_season_roster_membership(
            player["id"], ls_id, team["id"], actor_id=ADMIN)
        self.assertNotIn("error", m)
        expected = []
        for i in range(1, self.N_EVENTS):
            jersey = (i % 90) + 1  # always != previous, always in [1, 90]
            up = api.update_season_roster_membership(
                m["id"], jersey_number=jersey, actor_id=ADMIN)
            self.assertNotIn("error", up)
            expected.append(jersey)
        return m["id"], expected

    def _assert_chronological(self, events, expected_jerseys, label):
        self.assertEqual(len(events), self.N_EVENTS, label)
        self.assertEqual([e["action"] for e in events],
                         ["created"] + ["attributes_changed"] * (
                             self.N_EVENTS - 1), label)
        got = [e["detail"]["jersey_number"]["to"] for e in events[1:]]
        self.assertEqual(got, expected_jerseys, label)
        # Every event really does share the identical injected timestamp —
        # otherwise this test would trivially pass on ``at`` alone.
        self.assertEqual({e["at"] for e in events}, {self.FIXED.isoformat()},
                         label)

    def test_memory_sqlite_and_postgres_agree_on_chronological_order(self):
        stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            stores.append(("postgres", fresh_sql_store(url)))
        else:
            print("MembershipEventOrderingTest: " + _PG_SKIP)
        sequences = {}
        for label, store in stores:
            try:
                mid, expected = self._generate(store)
                events = store.events_for_membership(mid)
                dicts = [{"action": e.action,
                         "at": e.at.isoformat(),
                         "detail": e.detail} for e in events]
                self._assert_chronological(dicts, expected, label)
                # Compare only the ORDER-DEFINING content (action +, for the
                # jersey moves, the from/to pair) — never the "created"
                # event's ids, which are store-instance-specific by design
                # (each store mints its own player/team/league_season ids)
                # and would make cross-store equality meaningless.
                sequences[label] = [
                    (e["action"],
                     e["detail"].get("jersey_number") if e["action"] ==
                     "attributes_changed" else None)
                    for e in dicts]
            finally:
                if isinstance(store, SqlStore):
                    if store.backend != "sqlite":
                        store.reset_schema()
                    store.close()
        # Byte-identical chronological order across every store that ran.
        distinct = {repr(seq) for seq in sequences.values()}
        self.assertEqual(len(distinct), 1, sequences)

    def test_order_survives_restart_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, path)
        store = SqlStore(path)
        mid, expected = self._generate(store)
        before = [(e.action, e.seq, e.detail)
                 for e in store.events_for_membership(mid)]
        store.close()

        reopened = SqlStore(path)
        try:
            after = [(e.action, e.seq, e.detail)
                    for e in reopened.events_for_membership(mid)]
        finally:
            reopened.close()
        self.assertEqual(before, after)
        self.assertEqual(len(after), self.N_EVENTS)
        self.assertEqual([a for a, _, _ in after],
                         ["created"] + ["attributes_changed"] * (
                             self.N_EVENTS - 1))
        # seq strictly increasing (the real ordering key, persisted).
        seqs = [s for _, s, _ in after]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #205 Slice A membership lifecycle was NOT "
            "exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: the "
            "authoritative-active and season-Team jersey rules are partial "
            "unique indexes whose predicate/NULL semantics only a real "
            "engine evaluates, and the 052 backfill is multi-table SQL that "
            "must run under PostgreSQL's stricter typing. Set "
            "TEST_DATABASE_URL (run_parallel.py --postgres does) to run it.")


def _race_fixture(url):
    """Program -> Season -> Division -> Club -> TWO Teams both registered on
    the SAME LeagueSeason, plus one Player. Returns
    (team1_id, team2_id, ls_id, player_id)."""
    store = fresh_sql_store(url)
    api = ApiService(store)
    league, season, division, club, team, ls_id = _fixture(api)
    team2 = api.create_team(club["id"], division["id"], "Second",
                            actor_id=ADMIN)
    api.register_team_for_season(season["id"], team2["id"], division["id"],
                                 actor_id=ADMIN)
    player = _player(api, team["id"])
    store.close()
    return team["id"], team2["id"], ls_id, player["id"]


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class MembershipOpenStintRaceTest(unittest.TestCase):
    """Real TWO-CONNECTION PostgreSQL races proving migration 052's new
    ``ux_srm_open_player_league_season`` index (#205 review round 1 finding
    1) is a genuine engine-level backstop — bypassing the service layer
    ENTIRELY (no Team lock, no Season lock, no Player lock: two bare
    ``SqlStore`` connections racing a raw ``add_season_roster_membership``),
    exactly the shape the review's "Add a ... portable partial unique index"
    remedy targets. ``MembershipServiceCreateRaceTest`` below covers the
    same statuses THROUGH the service (where the Player lock added to
    ``create_season_roster_membership`` also protects it).

    Falsifier: `git stash` migration 052's new index (keep db_errors.py's
    translation) and every iteration below regresses to 2 winners / 0
    IntegrityConflictError.
    """

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _race_once(self, status: MembershipStatus, team1, team2, ls_id,
                   season_id, player_id, run_label):
        barrier = threading.Barrier(2)
        results = {}

        def attempt(name, team_id):
            store = SqlStore(self.url)
            try:
                barrier.wait(timeout=5)
                with store.transaction():
                    store.add_season_roster_membership(
                        SeasonRosterMembership(
                            id=f"srm_race_{run_label}_{name}",
                            player_id=player_id, league_season_id=ls_id,
                            season_id=season_id, team_id=team_id,
                            status=status, position=Position.FORWARD,
                            jersey_number=None))
                results[name] = "ok"
            except IntegrityConflictError as exc:
                results[name] = exc
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=("A", team1)),
                  threading.Thread(target=attempt, args=("B", team2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return results

    def _assert_race_holds(self, status):
        # Run several rounds so BOTH commit orders (whichever connection's
        # transaction the engine admits first) are actually exercised, not
        # just whichever happened to win once.
        winners_seen = set()
        for i in range(8):
            store = SqlStore(self.url)
            store.reset_schema()
            team1, team2, ls_id, player_id = _race_fixture(self.url)
            season_id = store.get_league_season(ls_id).season_id
            store.close()
            results = self._race_once(status, team1, team2, ls_id, season_id,
                                      player_id, f"{status.value}_{i}")
            winners = [k for k, v in results.items() if v == "ok"]
            losers = {k: v for k, v in results.items() if v != "ok"}
            self.assertEqual(len(winners), 1,
                             (status.value, i, results))
            self.assertEqual(len(losers), 1, (status.value, i, results))
            winners_seen.add(winners[0])
            loser_exc = next(iter(losers.values()))
            self.assertIsInstance(loser_exc, IntegrityConflictError,
                                  (status.value, i, loser_exc))
            self.assertEqual(loser_exc.details["reason"],
                             "membership_open_conflict", (status.value, i))
            self.assertEqual(loser_exc.details["player_id"], player_id,
                             (status.value, i))
            self.assertEqual(loser_exc.details["league_season_id"], ls_id,
                             (status.value, i))
            # Zero-write proof: exactly one row landed for this player.
            checker = SqlStore(self.url)
            try:
                rows = [m for m in checker.all_season_roster_memberships()
                       if m.player_id == player_id]
                self.assertEqual(len(rows), 1, (status.value, i,
                                                [r.id for r in rows]))
            finally:
                checker.close()
        # Both A and B must have won at least once across the 8 rounds —
        # "both commit orders" the review requires, not just one direction.
        self.assertEqual(winners_seen, {"A", "B"},
                         (status.value, "did not observe both commit orders",
                          winners_seen))

    def test_applicant_on_different_teams_exactly_one_wins(self):
        self._assert_race_holds(MembershipStatus.APPLICANT)

    def test_affiliate_on_different_teams_exactly_one_wins(self):
        self._assert_race_holds(MembershipStatus.AFFILIATE)

    def test_injured_on_different_teams_exactly_one_wins(self):
        self._assert_race_holds(MembershipStatus.INJURED)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class MembershipServiceCreateRaceTest(unittest.TestCase):
    """Real TWO-CONNECTION PostgreSQL races THROUGH the service
    (``create_season_roster_membership``), proving the full defended path —
    the new Player row lock plus migration 052's new index as backstop
    (#205 review round 1 finding 1) — for applicant/affiliate/inactive on
    DIFFERENT registered Teams of the SAME LeagueSeason, both commit orders,
    exactly one success, and ZERO membership/event/audit rows for the loser
    (the required coverage the review names).
    """

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _race_once(self, status, team1, team2, ls_id, player_id, run_label):
        barrier = threading.Barrier(2)
        results = {}

        def attempt(name, team_id):
            store = SqlStore(self.url)
            api = ApiService(store)
            try:
                barrier.wait(timeout=5)
                res = api.create_season_roster_membership(
                    player_id, ls_id, team_id, status=status.value,
                    jersey_number=None, reason=f"race-{run_label}",
                    actor_id=f"actor_{name}")
                results[name] = res
            finally:
                store.close()

        threads = [threading.Thread(target=attempt, args=("A", team1)),
                  threading.Thread(target=attempt, args=("B", team2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return results

    def _assert_race_holds(self, status):
        winners_seen = set()
        for i in range(8):
            team1, team2, ls_id, player_id = _race_fixture(self.url)
            run_label = f"{status.value}_{i}"
            results = self._race_once(status, team1, team2, ls_id, player_id,
                                      run_label)
            winners = [k for k, v in results.items()
                      if isinstance(v, dict) and "error" not in v]
            losers = {k: v for k, v in results.items()
                     if not (isinstance(v, dict) and "error" not in v)}
            self.assertEqual(len(winners), 1, (status.value, i, results))
            self.assertEqual(len(losers), 1, (status.value, i, results))
            winners_seen.add(winners[0])
            loser = next(iter(losers.values()))
            self.assertIn("error", loser, (status.value, i, loser))
            self.assertEqual(loser["error"]["details"]["reason"],
                             "membership_open_conflict", (status.value, i))
            # Zero-write proof, service-level: exactly one membership, one
            # "created" event, one setup_audit_logs row for this player —
            # the loser's @_transactional rollback left NOTHING behind.
            checker = SqlStore(self.url)
            try:
                members = [m for m in checker.all_season_roster_memberships()
                          if m.player_id == player_id]
                self.assertEqual(len(members), 1, (status.value, i))
                events = checker.events_for_membership(members[0].id)
                self.assertEqual([e.action for e in events], ["created"],
                                 (status.value, i))
                audits = [a for a in checker.all_setup_audit()
                         if a.entity_type == "season_roster_membership"
                         and a.entity_id == members[0].id]
                self.assertEqual(len(audits), 1, (status.value, i))
            finally:
                checker.close()
        self.assertEqual(winners_seen, {"A", "B"},
                         (status.value, "did not observe both commit orders",
                          winners_seen))

    def test_applicant_on_different_teams_exactly_one_succeeds(self):
        self._assert_race_holds(MembershipStatus.APPLICANT)

    def test_affiliate_on_different_teams_exactly_one_succeeds(self):
        self._assert_race_holds(MembershipStatus.AFFILIATE)

    def test_inactive_on_different_teams_exactly_one_succeeds(self):
        self._assert_race_holds(MembershipStatus.INACTIVE)


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
