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
  * MIGRATION 059 — deterministic backfill (active players x non-archived
    registered Seasons, values copied, ``srm_legacy_`` compatibility-map
    ids, NULL ``effective_from``, EMPTY event history, GuardianLinks
    untouched), preflight that REPORTS ambiguous/dangling rows and aborts
    with the database left byte-identical (no ledger row, no tables), and
    an idempotent re-run once the operator fixes the named rows.
  * DB ENFORCEMENT — migration 059's two partial unique indexes hold under
    a direct store write that bypasses every service pre-check, and the
    violation is translated to the SAME stable conflict the pre-check
    raises.

THREE STORES. InMemoryStore, SQLite and PostgreSQL. The uniqueness rules are
a dict-scan on one store and real partial-index semantics on the others; the
backfill is real SQL (JOINs over players/teams/registrations/league_seasons/
seasons) that only an actual engine can execute. A SKIP IS NOT A PASS — the
PostgreSQL classes announce loudly when TEST_DATABASE_URL is unset.

FALSIFIERS (each measured while developing this slice):
  * delete the ``WHERE p.is_active = 1`` line of migration 059's INSERT and
    the inactive-player backfill tests turn red on SQLite and PostgreSQL;
  * delete ``AND s.status = 'active'`` and the archived-season tests turn
    red the same way;
  * unregister ``059_season_roster_membership`` from _PRE_MIGRATION_CHECKS
    and the preflight-abort tests turn red (the dirty upgrade "succeeds");
  * drop either partial unique index from the migration and the
    direct-store conflict tests turn red on both SQL engines.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import end_membership_directly, fresh_sql_store, race_with_forced_order

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
    _MISSING_OR_UNEQUAL,
    MigrationDataError,
    assert_season_roster_membership_backfill_ready,
    find_active_players_with_dangling_league_season_parents,
    find_active_players_with_dangling_registration_target,
    find_active_players_with_missing_team,
    find_active_players_with_program_mismatch,
    find_active_players_with_team_league_mismatch,
    find_active_players_with_team_program_mismatch,
    find_teams_with_duplicate_active_season_registrations,
)
from hockey_scheduler.store.sql_store import migrate

ADMIN = "setup_admin"
_VERSION = "059_season_roster_membership"


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
                # #205 review round 2 (owner ruling, overriding round 1
                # finding 5's actor_id+reason floor): reaching "released"
                # through this method is now refused UNCONDITIONALLY, zero
                # write — proven here on the live, non-terminal membership
                # this subTest already has, before falling back to a direct
                # store write for the "already terminal, immutable" half of
                # this test below (which is a DIFFERENT rule, untouched).
                before_events = len(api.list_season_roster_membership_events(
                    m["id"])["events"])
                before_audit = len(api.store.all_setup_audit())
                refused = api.set_season_roster_membership_status(
                    m["id"], "released", reason="cut", actor_id=ADMIN)
                self.assertEqual(refused["error"]["code"], "forbidden",
                                 (label, refused))
                self.assertEqual(
                    refused["error"]["details"]["reason"],
                    "terminal_transition_not_authorized", (label, refused))
                self.assertEqual(
                    len(api.list_season_roster_membership_events(
                        m["id"])["events"]),
                    before_events, label)
                self.assertEqual(len(api.store.all_setup_audit()),
                                 before_audit, label)
                self.assertIs(
                    api.store.get_season_roster_membership(m["id"]).status,
                    MembershipStatus.ACTIVE, label)
                rel = end_membership_directly(api.store, m["id"], "released")
                self.assertIs(rel.status, MembershipStatus.RELEASED, label)
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
                # Direct store write, not the now-unconditionally-refused
                # set_season_roster_membership_status (#205 review round 2
                # owner ruling) — this test's subject is that RELEASING
                # frees the jersey number, not the transition method's own
                # (now removed) authorization.
                end_membership_directly(api.store, m1["id"], "released")
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


class MembershipTerminalTransitionRefusedTest(unittest.TestCase):
    """#205 review round 2 — OWNER PRODUCT RULING, overriding round 1
    finding 5's shipped "actor_id + reason" floor
    (formerly MembershipTerminalTransitionFloorTest, REWORKED, not merely
    renamed: every test below now proves the OPPOSITE of what this class
    used to prove — that an actor_id+reason combination that satisfied the
    old floor now ALSO gets refused).

    The floor was a validated-INPUT check, not authorization: any caller
    could satisfy it by supplying an arbitrary non-blank string for each,
    so it never actually stopped an unauthorized release/transfer — only a
    silent/anonymous/unreasoned one. ``set_season_roster_membership_status``
    now UNCONDITIONALLY refuses every terminal transition (released AND
    transferred) — no actor_id/reason combination, including ones that used
    to succeed under the old floor, reaches a write. The schema and event
    model stay fully capable of representing released/transferred (the enum
    values are untouched, and a direct store write still reaches them, see
    the last test below) — only SETTING one through this method is refused.
    See set_season_roster_membership_status's and its facade pass-through's
    docstrings, and the PR body, for the explicit ruling."""

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

    def test_released_refused_unconditionally_zero_write(self):
        for label, api, m in self._each():
            with self.subTest(backend=label):
                before = self._snapshot(api, m["id"])
                for kwargs in (
                        {},                                    # neither
                        {"actor_id": ADMIN},                    # actor only
                        {"reason": "cut"},                      # reason only
                        {"actor_id": "", "reason": ""},         # blank
                        {"actor_id": "   ", "reason": "   "},   # whitespace
                        # The EXACT combination round 1's floor accepted —
                        # the whole point of the owner ruling is that this
                        # no longer succeeds either.
                        {"actor_id": ADMIN, "reason": "cut"}):
                    res = api.set_season_roster_membership_status(
                        m["id"], "released", **kwargs)
                    self.assertEqual(res["error"]["code"], "forbidden",
                                     (label, kwargs, res))
                    self.assertEqual(
                        res["error"]["details"]["reason"],
                        "terminal_transition_not_authorized",
                        (label, kwargs, res))
                    self.assertEqual(self._snapshot(api, m["id"]), before,
                                     (label, kwargs))

    def test_transferred_refused_unconditionally_zero_write(self):
        for label, api, m in self._each():
            with self.subTest(backend=label):
                before = self._snapshot(api, m["id"])
                res = api.set_season_roster_membership_status(
                    m["id"], "transferred", reason="moved club",
                    actor_id=ADMIN)
                self.assertEqual(res["error"]["code"], "forbidden",
                                 (label, res))
                self.assertEqual(
                    res["error"]["details"]["reason"],
                    "terminal_transition_not_authorized", (label, res))
                self.assertEqual(self._snapshot(api, m["id"]), before, label)

    def test_non_terminal_transition_unaffected(self):
        # The refusal is scoped to TERMINAL targets only — parking a
        # membership (a fully reversible, non-terminal transition) still
        # needs neither an actor nor a reason, exactly as before the ruling.
        for label, api, m in self._each():
            with self.subTest(backend=label):
                res = api.set_season_roster_membership_status(
                    m["id"], "inactive")
                self.assertNotIn("error", res, (label, res))

    def test_schema_still_represents_terminal_status_via_direct_write(self):
        # The enum/schema and event model stay fully capable of
        # representing released/transferred (#212's later slice ships the
        # authorized path that reaches them through the service); only
        # THIS method's surface is closed. A membership terminated directly
        # at the store layer (the owner ruling's prescribed rework for
        # tests that need a terminal PRECONDITION — see
        # end_membership_directly, used throughout this module) is still
        # immutable afterward, the SAME pre-existing rule
        # test_status_lifecycle_terminal_immutability_and_new_stint proves
        # above, unaffected by this ruling.
        for label, api, m in self._each():
            with self.subTest(backend=label):
                released = end_membership_directly(
                    api.store, m["id"], "released")
                self.assertIs(released.status, MembershipStatus.RELEASED,
                              label)
                frozen = api.set_season_roster_membership_status(
                    m["id"], "active", actor_id=ADMIN)
                self.assertEqual(frozen["error"]["details"]["reason"],
                                 "membership_terminal", label)


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


def _downgrade_059(store):
    """Simulate a pre-059 database: drop the two tables (their indexes go
    with them) and un-record the version, so the next migrate() re-runs 059
    against whatever data the test has planted."""
    with store.transaction():
        cur = store.conn.cursor()
        cur.execute("DROP TABLE IF EXISTS season_roster_membership_events")
        cur.execute("DROP TABLE IF EXISTS season_roster_memberships")
        cur.execute(store.dialect.sql(
            "DELETE FROM schema_migrations WHERE version = ?"), (_VERSION,))


def _seed_legacy(api):
    """A pre-059-shaped world: one active Season with a registered Team, an
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
    """Migration 059's deterministic backfill + preflight + rollback, on
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
                _downgrade_059(store)
                migrate(store.conn, store.dialect)  # re-applies 059

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
                _downgrade_059(store)

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
                _downgrade_059(store)

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
    """Migration 059's preflight validates the FULL registration ->
    LeagueSeason -> Season/League spine, Team<->LeagueSeason League
    coherence, League<->Season Program coherence (#205 review round 1
    finding 3), AND the Team's OWN Program against its League's (#205
    review round 2 finding 1 — a gap round 1's checks left open: a Team's
    ``program_id`` is a separate column from its ``league_id``, so even a
    Team whose League/LeagueSeason/Season chain is perfectly coherent can
    still disagree with that chain on Program), before any DDL.

    None of these five corruption shapes touch a column any FK constraint
    actually covers (``season_team_registrations.league_season_id``,
    ``league_seasons.season_id``/``league_id``, ``teams.league_id`` and
    ``teams.program_id`` were all added by plain
    ``ALTER TABLE ... ADD COLUMN`` — no ``FOREIGN KEY``), so no FK-disable
    dance is needed to plant them; that is exactly WHY they can reach an
    ordinary UPDATE undetected in the first place, and exactly why this
    preflight exists.
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
                _downgrade_059(store)

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
                _downgrade_059(store)

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
                _downgrade_059(store)

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
                _downgrade_059(store)

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

    def test_team_program_mismatch_aborts_and_repairs(self):
        # A fresh external review's exact scenario (#205 review round 2
        # finding 1): change a candidate Team's OWN program_id to another
        # existing Program while its League, LeagueSeason and Season stay
        # otherwise valid and mutually coherent — team.league_id still
        # matches ls.league_id (find_active_players_with_team_league_mismatch
        # stays clean) and league.program_id still matches season.program_id
        # (find_active_players_with_program_mismatch stays clean too), so
        # only a check that reads teams.program_id directly can catch it.
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                other_program = api.create_program("Other Program",
                                                    actor_id=ADMIN)
                original_team_program_id = api.store.get_team(
                    ids["team"]).program_id
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE teams SET program_id = ? WHERE id = ?"),
                    (other_program["id"], ids["team"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_059(store)

                # Both ROUND 1 spine checks stay clean — this is a NEW gap,
                # not a duplicate of either.
                self.assertEqual(
                    find_active_players_with_team_league_mismatch(
                        store.conn), [], label)
                self.assertEqual(
                    find_active_players_with_program_mismatch(store.conn),
                    [], label)
                found = find_active_players_with_team_program_mismatch(
                    store.conn)
                self.assertEqual(len(found), 1, (label, found))
                self.assertEqual(found[0][0], ids["active"], (label, found))
                self.assertEqual(found[0][1], ids["team"], (label, found))
                self.assertEqual(found[0][2], other_program["id"],
                                 (label, found))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_clean_rollback(store, label)
                # Repair: restore the Team's original Program.
                cur.execute(store.dialect.sql(
                    "UPDATE teams SET program_id = ? WHERE id = ?"),
                    (original_team_program_id, ids["team"]))
                if store.backend == "sqlite":
                    store.conn.commit()
                migrate(store.conn, store.dialect)
                rows = store.all_season_roster_memberships()
                self.assertEqual(
                    [(m.player_id, m.league_season_id) for m in rows],
                    [(ids["active"], ids["ls"])], label)
            finally:
                self._cleanup(store)


# --------------------------------------------------------------------------- #
# #205 review round 3 blocker 1 — a MISSING scope-spine key is a violation,    #
# not an exemption. Every coherence check in MembershipBackfillSpineTest above #
# compared two keys for INEQUALITY, so a NULL on either side was never         #
# reported: two checks excluded it outright (AND t.league_id IS NOT NULL, AND  #
# t.program_id IS NOT NULL) and the third let SQL's three-valued logic drop it #
# silently (lg.program_id != s.program_id is UNKNOWN, not TRUE, against NULL). #
# --------------------------------------------------------------------------- #

# One entry per scope-spine key the owner named. ``sql`` NULLs it on the
# single active backfill candidate _seed_legacy plants; ``original`` recovers
# the value an operator would restore; ``names`` are ids the bounded
# diagnostic must name so the operator can find the row.
_NULL_SPINE_KEYS = {
    "teams.league_id": {
        "sql": "UPDATE teams SET league_id = NULL WHERE id = ?",
        "target": lambda api, ids: ids["team"],
        "original": lambda api, ids: api.store.get_league_season(
            ids["ls"]).league_id,
        "finder": "find_active_players_with_team_league_mismatch",
    },
    "teams.program_id": {
        "sql": "UPDATE teams SET program_id = NULL WHERE id = ?",
        "target": lambda api, ids: ids["team"],
        "original": lambda api, ids: api.store.get_season(
            ids["season"]).program_id,
        "finder": "find_active_players_with_team_program_mismatch",
    },
    "leagues.program_id": {
        "sql": "UPDATE leagues SET program_id = NULL WHERE id = ?",
        "target": lambda api, ids: api.store.get_league_season(
            ids["ls"]).league_id,
        "original": lambda api, ids: api.store.get_season(
            ids["season"]).program_id,
        "finder": "find_active_players_with_program_mismatch",
    },
    "seasons.program_id": {
        "sql": "UPDATE seasons SET program_id = NULL WHERE id = ?",
        "target": lambda api, ids: ids["season"],
        "original": lambda api, ids: api.store.get_league(
            api.store.get_league_season(ids["ls"]).league_id).program_id,
        "finder": "find_active_players_with_program_mismatch",
    },
}


class MembershipBackfillNullSpineKeyTest(unittest.TestCase):
    """Migration 059's preflight treats a MISSING scope-spine key as a
    violation on every active backfill candidate — ``teams.league_id``,
    ``teams.program_id``, ``leagues.program_id`` and ``seasons.program_id``
    alike (#205 review round 3 blocker 1).

    Each of the four was demonstrated on THIS branch's prior head, on SQLite
    AND PostgreSQL: NULLed on an otherwise-perfectly-valid active candidate,
    ``assert_season_roster_membership_backfill_ready`` returned clean and
    migration 059 applied and backfilled a membership onto a spine with no
    League (or no Program) at all. That spine is not one the live system
    produces: ``register_team_for_season`` ASSIGNS a League rather than
    leaving an actively-registered Team league-less (#283 Slice E rule 2),
    refuses a program-less Team on the canonical path
    (``team_program_mismatch``), and ``_link_league_season`` refuses a
    League/Season pair when one side has no Program
    (``league_season_program_mismatch``) — all three verified by execution
    against this head while the corresponding preflight stayed silent.

    THE MATRIX, in full: 4 keys x 2 SQL engines = 8 fixtures, each asserting
    (a) a BOUNDED, row-level diagnostic naming the offending row, (b) no 059
    ledger row, no 059 tables and therefore no partial rows after the
    refusal, and (c) that repairing the one named field and re-running the
    upgrade applies the EXACT expected membership set.
    """

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def _assert_no_ledger_tables_or_rows(self, store, label):
        """(b) — the refusal happened before ANY DDL: no ledger row, neither
        059 table, and so not one partial row of either."""
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(
            "SELECT COUNT(*) AS n FROM schema_migrations WHERE version = ?"),
            (_VERSION,))
        self.assertEqual(cur.fetchone()["n"], 0, label)
        for table in ("season_roster_memberships",
                      "season_roster_membership_events"):
            if store.backend == "sqlite":
                cur.execute(
                    "SELECT COUNT(*) AS n FROM sqlite_master WHERE "
                    "type='table' AND name=?", (table,))
            else:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_name=%s", (table,))
            self.assertEqual(cur.fetchone()["n"], 0, (label, table))
            # A dropped table cannot hold a partial row: prove the table is
            # genuinely gone rather than merely absent from the catalogue
            # view, by showing a direct read of it fails.
            with self.assertRaises(Exception, msg=(label, table)):
                probe = store.conn.cursor()
                probe.execute(f"SELECT * FROM {table}")
                probe.fetchall()
            if store.backend != "sqlite":
                store.conn.rollback()

    def _run_null_key_case(self, field, spec, label, url):
        """One matrix cell: NULL this key on the single active candidate,
        prove the refusal + its diagnostic + the clean rollback, repair the
        one named field, and prove the re-run produces the exact set."""
        case = (label, field)
        store = _fresh(url)
        try:
            api = ApiService(store)
            ids = _seed_legacy(api)
            target = spec["target"](api, ids)
            original = spec["original"](api, ids)
            self.assertIsNotNone(original, case)
            cur = store.conn.cursor()
            cur.execute(store.dialect.sql(spec["sql"]), (target,))
            if store.backend == "sqlite":
                store.conn.commit()
            _downgrade_059(store)

            # The check that OWNS this key reports the row...
            finder = globals()[spec["finder"]]
            found = finder(store.conn)
            self.assertEqual(len(found), 1, (case, found))
            self.assertEqual(found[0][0], ids["active"], (case, found))
            self.assertIn(None, found[0], (case, found))
            # ...and only the ACTIVE candidate is reported; the
            # inactive player is not a backfill candidate at all.
            self.assertNotIn(ids["inactive"],
                             [row[0] for row in found], case)

            # (a) BOUNDED, ROW-LEVEL diagnostic naming the bad row.
            with self.assertRaises(MigrationDataError, msg=case) as ctx:
                migrate(store.conn, store.dialect)
            message = str(ctx.exception)
            self.assertIn(ids["active"], message, case)
            self.assertIn("MISSING", message, case)
            self.assertIn("1 active player(s)", message, case)
            self.assertNotIn(ids["inactive"], message, case)
            self.assertNotIn("more", message, case)

            # (b) nothing was written before the refusal.
            self._assert_no_ledger_tables_or_rows(store, case)

            # (c) repair the ONE named field; the re-run applies the
            # EXACT expected membership set — the active player in
            # the active Season, and nothing else.
            cur = store.conn.cursor()
            cur.execute(store.dialect.sql(
                spec["sql"].replace("= NULL", "= ?")),
                (original, target))
            if store.backend == "sqlite":
                store.conn.commit()
            assert_season_roster_membership_backfill_ready(store.conn)
            migrate(store.conn, store.dialect)
            rows = store.all_season_roster_memberships()
            self.assertEqual(
                sorted((m.id, m.player_id, m.league_season_id,
                        m.team_id, m.status.value) for m in rows),
                [(f"srm_legacy_{ids['active']}_{ids['ls']}",
                  ids["active"], ids["ls"], ids["team"], "active")],
                case)
        finally:
            self._cleanup(store)

    def test_each_null_spine_key_aborts_and_repairs(self):
        checked = 0
        for field, spec in _NULL_SPINE_KEYS.items():
            for label, url in _sql_backends():
                with self.subTest(backend=label, spine_key=field):
                    self._run_null_key_case(field, spec, label, url)
                checked += 1
        # 4 keys x SQLite (+ PostgreSQL when configured). Pinned against the
        # ENVIRONMENT, not against len(_sql_backends()) -- that earlier form
        # could never fire, because `checked` is incremented by a loop over
        # the very list it was compared to, so both sides moved together and
        # a backend dropping out shrank the matrix silently. Mirrors
        # ParkedRevivalSpineTest's pin, which uses this same independent
        # source.
        expected = 4 * (2 if os.environ.get("TEST_DATABASE_URL") else 1)
        self.assertEqual(checked, expected)

    def test_both_program_keys_null_is_still_reported(self):
        """Two MISSING keys are not agreement. This is precisely where
        ``IS DISTINCT FROM`` — the null-safe operator a reviewer might reach
        for — would go wrong: ``NULL IS DISTINCT FROM NULL`` is FALSE, so a
        League with no Program registered into a Season with no Program
        would pass as coherent. The explicit portable form reports it."""
        for label, url in _sql_backends():
          with self.subTest(backend=label):
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                league_id = api.store.get_league_season(ids["ls"]).league_id
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE leagues SET program_id = NULL WHERE id = ?"),
                    (league_id,))
                cur.execute(store.dialect.sql(
                    "UPDATE seasons SET program_id = NULL WHERE id = ?"),
                    (ids["season"],))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_059(store)

                found = find_active_players_with_program_mismatch(store.conn)
                self.assertEqual(len(found), 1, (label, found))
                self.assertEqual(found[0][0], ids["active"], (label, found))
                self.assertEqual(found[0][3], None, (label, found))
                self.assertEqual(found[0][5], None, (label, found))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self._assert_no_ledger_tables_or_rows(store, label)
            finally:
                self._cleanup(store)

    def test_diagnostic_stays_bounded_at_twenty_rows(self):
        """Row-level does not mean unbounded: with far more offending rows
        than the cap, the message names 20 and summarises the rest, exactly
        as every other check in this module does. Proves the diagnostic
        does not dump the whole table."""
        for label, url in _sql_backends():
          with self.subTest(backend=label):
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                for n in range(25):
                    api.create_player(ids["team"], f"Extra{n}", "forward",
                                      jersey_number=None, actor_id=ADMIN)
                cur = store.conn.cursor()
                cur.execute(store.dialect.sql(
                    "UPDATE teams SET league_id = NULL WHERE id = ?"),
                    (ids["team"],))
                if store.backend == "sqlite":
                    store.conn.commit()
                _downgrade_059(store)

                found = find_active_players_with_team_league_mismatch(
                    store.conn)
                self.assertEqual(len(found), 26, (label, len(found)))
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                message = str(ctx.exception)
                self.assertIn("26 active player(s)", message, label)
                self.assertIn("(+6 more)", message, label)
                self.assertEqual(message.count("player player_"), 20,
                                 (label, message))
                self._assert_no_ledger_tables_or_rows(store, label)
            finally:
                self._cleanup(store)

    def test_missing_or_unequal_form_is_identical_on_both_engines(self):
        """The portability trap, pinned by EXECUTION rather than reasoning.

        This store runs on SQLite and PostgreSQL, and the null-safe
        operators are not interchangeable: SQLite spells it ``IS NOT`` (a
        hard syntax error on PostgreSQL) and PostgreSQL spells it
        ``IS DISTINCT FROM`` (which also gives the WRONG answer for a scope
        spine, treating two missing keys as agreement). ``_MISSING_OR_
        UNEQUAL`` therefore uses only ``IS NULL``/``!=``/``OR``. This test
        runs the five-row truth table through the real engines and requires
        the row sets to match, so a future "simplification" to either
        engine-specific operator fails here."""
        results = {}
        for label, url in _sql_backends():
            store = _fresh(url)
            try:
                cur = store.conn.cursor()
                cur.execute("CREATE TABLE null_cmp_probe "
                            "(label TEXT, a TEXT, b TEXT)")
                for row in (("both_null", None, None), ("a_null", None, "x"),
                            ("b_null", "x", None), ("equal", "x", "x"),
                            ("differ", "x", "y")):
                    cur.execute(store.dialect.sql(
                        "INSERT INTO null_cmp_probe VALUES (?, ?, ?)"), row)
                if store.backend == "sqlite":
                    store.conn.commit()
                cur.execute(
                    "SELECT label FROM null_cmp_probe WHERE "
                    + _MISSING_OR_UNEQUAL.format(a="a", b="b")
                    + " ORDER BY label")
                results[label] = [r["label"] for r in cur.fetchall()]
                # The OLD form, for contrast: it drops every NULL row on
                # both engines — identical, and identically wrong.
                cur.execute("SELECT label FROM null_cmp_probe WHERE a != b")
                self.assertEqual([r["label"] for r in cur.fetchall()],
                                 ["differ"], label)
                cur.execute("DROP TABLE null_cmp_probe")
                if store.backend == "sqlite":
                    store.conn.commit()
            finally:
                self._cleanup(store)
        for label, got in results.items():
            self.assertEqual(
                got, ["a_null", "b_null", "both_null", "differ"], label)
        # Same independent pin: when PostgreSQL is configured BOTH engines
        # must have produced a row set, otherwise the cross-engine equality
        # assertion below would silently evaporate on a one-engine run.
        if os.environ.get("TEST_DATABASE_URL"):
            self.assertEqual(sorted(results), ["postgres", "sqlite"], results)
        self.assertEqual(len(set(map(tuple, results.values()))), 1, results)


class NullLeagueLayerAgreementTest(unittest.TestCase):
    """#205 review round 3 blocker 3 — migration 059 and the LIVE SERVICE
    must agree about the NULL-``teams.league_id`` shape, on the SAME
    database, in BOTH directions.

    That disagreement was the whole substance of the finding. After blocker
    1, 059's preflight REFUSED to backfill a league-less Team
    (``_MISSING_OR_UNEQUAL`` reports a missing key as a violation), while
    ``create_season_roster_membership`` and the parked-revival path still
    guarded League coherence as ``if team.league_id and ls.league_id !=
    team.league_id`` — a FALSY-SKIP that waved the identical shape through.
    The migration therefore refused to materialize exactly the row the live
    system would happily mint and revive.

    This test runs BOTH layers against ONE store, per SQL engine:

      * SERVICE, with 059 applied: creating a membership on the league-less
        Team is refused with ``membership_league_mismatch``, and so is
        reviving a membership parked BEFORE the League went missing. Zero
        writes either way.
      * MIGRATION, on the same rows: downgrade 059 and re-run it; the
        preflight aborts with a bounded diagnostic naming the same Team's
        active player and the word MISSING.
      * THE CONVERSE, so "agreement" is not just "both refuse everything":
        repair the one field and both layers accept — the service creates,
        and 059 backfills the exact expected membership set.
    """

    _NULL_TEAM_LEAGUE = "UPDATE teams SET league_id = NULL WHERE id = ?"

    def _cleanup(self, store):
        if store.backend != "sqlite":
            store.reset_schema()
        store.close()

    def _write(self, store, sql, params):
        cur = store.conn.cursor()
        cur.execute(store.dialect.sql(sql), params)
        if store.backend == "sqlite":
            store.conn.commit()

    def _membership_rows(self, store, player_id):
        return [m for m in store.all_season_roster_memberships()
                if m.player_id == player_id]

    def test_service_and_migration_agree_on_the_null_league_shape(self):
        checked = 0
        for label, url in _sql_backends():
          with self.subTest(backend=label):
            store = _fresh(url)
            try:
                api = ApiService(store)
                ids = _seed_legacy(api)
                original_league = api.store.get_league_season(
                    ids["ls"]).league_id
                self.assertIsNotNone(original_league, label)

                # A membership parked while the spine was still WHOLE.
                parked = api.create_season_roster_membership(
                    ids["active"], ids["ls"], ids["team"],
                    status="applicant", jersey_number=None, actor_id=ADMIN)
                self.assertNotIn("error", parked, (label, parked))
                self.assertNotIn(
                    "error", api.set_season_roster_membership_status(
                        parked["id"], "inactive", actor_id=ADMIN), label)

                # The Team loses its permanent League out of band.
                self._write(store, self._NULL_TEAM_LEAGUE, (ids["team"],))
                self.assertIsNone(store.get_team(ids["team"]).league_id, label)

                # --- LAYER 1: the live service refuses to MINT one...
                audits_before = len(store.all_setup_audit())
                res = api.create_season_roster_membership(
                    ids["inactive"], ids["ls"], ids["team"],
                    status="applicant", jersey_number=None, actor_id=ADMIN)
                self.assertEqual(res["error"]["details"]["reason"],
                                 "membership_league_mismatch", (label, res))
                self.assertEqual(
                    self._membership_rows(store, ids["inactive"]), [], label)

                # ...and refuses to REVIVE the parked one.
                for target in ("applicant", "affiliate", "active"):
                    revived = api.set_season_roster_membership_status(
                        parked["id"], target, actor_id=ADMIN)
                    self.assertEqual(
                        revived["error"]["details"]["reason"],
                        "membership_league_mismatch", (label, target, revived))
                    self.assertEqual(
                        store.get_season_roster_membership(
                            parked["id"]).status.value, "inactive",
                        (label, target))
                # Zero writes across every refusal above.
                self.assertEqual(len(store.all_setup_audit()), audits_before,
                                 label)

                # --- LAYER 2: migration 059 refuses to BACKFILL it, on the
                # very same rows.
                found = find_active_players_with_team_league_mismatch(
                    store.conn)
                self.assertEqual([row[0] for row in found], [ids["active"]],
                                 (label, found))
                _downgrade_059(store)
                with self.assertRaises(MigrationDataError, msg=label) as ctx:
                    migrate(store.conn, store.dialect)
                self.assertIn(ids["active"], str(ctx.exception), label)
                self.assertIn("MISSING", str(ctx.exception), label)

                # --- THE CONVERSE: repair the ONE field and BOTH layers
                # accept. Agreement, not mutual paralysis.
                self._write(
                    store, "UPDATE teams SET league_id = ? WHERE id = ?",
                    (original_league, ids["team"]))
                assert_season_roster_membership_backfill_ready(store.conn)
                migrate(store.conn, store.dialect)
                self.assertEqual(
                    sorted((m.player_id, m.league_season_id)
                           for m in store.all_season_roster_memberships()),
                    [(ids["active"], ids["ls"])], label)
                # The service accepts the repaired spine too. (The backfill
                # above already holds the active player's open stint, so
                # this uses the OTHER player, exactly as the refusal did.)
                ok = ApiService(store).create_season_roster_membership(
                    ids["inactive"], ids["ls"], ids["team"],
                    status="applicant", jersey_number=None, actor_id=ADMIN)
                self.assertNotIn("error", ok, (label, ok))
                self.assertEqual(ok["status"], "applicant", label)
            finally:
                self._cleanup(store)
          checked += 1
        # SQLite always; PostgreSQL when configured. Pinned against the
        # ENVIRONMENT, never against len(_sql_backends()) -- that form moves
        # with the loop and can never fire.
        expected = 2 if os.environ.get("TEST_DATABASE_URL") else 1
        self.assertEqual(checked, expected)


class MembershipIndexEnforcementTest(unittest.TestCase):
    """Migration 059's partial unique indexes hold when the service layer is
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
            "engine evaluates, and the 059 backfill is multi-table SQL that "
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
    """Real TWO-CONNECTION PostgreSQL races proving migration 059's new
    ``ux_srm_open_player_league_season`` index (#205 review round 1 finding
    1) is a genuine engine-level backstop — bypassing the service layer
    ENTIRELY (no Team lock, no Season lock, no Player lock: two bare
    ``SqlStore`` connections racing a raw ``add_season_roster_membership``),
    exactly the shape the review's "Add a ... portable partial unique index"
    remedy targets. ``MembershipServiceCreateRaceTest`` below covers the
    same statuses THROUGH the service (where the Player lock added to
    ``create_season_roster_membership`` also protects it).

    #205 review round 2 (owner ruling + review finding 3) — REDESIGNED: for
    EACH status, two forced orderings via helpers.race_with_forced_order,
    gated on the exact contested write (``add_season_roster_membership``),
    instead of 8 simultaneous-start rounds hoping both commit orders
    occurred. The two racers write to the SAME (player_id,
    league_season_id) unique-index scope, so forcing which side's INSERT
    reaches PostgreSQL first deterministically decides the winner: real
    PostgreSQL unique-index semantics make a second, concurrent, competing
    INSERT block until the first's transaction resolves, then raise once it
    sees the committed row (see race_with_forced_order's docstring).

    Falsifier: `git stash` migration 059's new index (keep db_errors.py's
    translation) and both forced orderings below regress to the SECOND
    insert also succeeding (0 IntegrityConflictError) instead of raising.
    """

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _op(self, team_id, status, ls_id, season_id, player_id, name):
        def op(store):
            with store.transaction():
                store.add_season_roster_membership(SeasonRosterMembership(
                    id=f"srm_race_{name}", player_id=player_id,
                    league_season_id=ls_id, season_id=season_id,
                    team_id=team_id, status=status, position=Position.FORWARD,
                    jersey_number=None))
            return "ok"
        return op

    def _assert_ordering(self, status, team1_first, ordering_label):
        store = SqlStore(self.url)
        store.reset_schema()
        team1, team2, ls_id, player_id = _race_fixture(self.url)
        season_id = store.get_league_season(ls_id).season_id
        store.close()
        first_team, second_team = (
            (team1, team2) if team1_first else (team2, team1))
        first_res, second_res = race_with_forced_order(
            self.url, "add_season_roster_membership",
            self._op(first_team, status, ls_id, season_id, player_id,
                     f"{status.value}_{ordering_label}_first"),
            self._op(second_team, status, ls_id, season_id, player_id,
                     f"{status.value}_{ordering_label}_second"))
        self.assertEqual(first_res, "ok",
                         (status.value, ordering_label, first_res))
        self.assertIsInstance(second_res, IntegrityConflictError,
                              (status.value, ordering_label, second_res))
        self.assertEqual(second_res.details["reason"],
                         "membership_open_conflict",
                         (status.value, ordering_label))
        self.assertEqual(second_res.details["player_id"], player_id,
                         (status.value, ordering_label))
        self.assertEqual(second_res.details["league_season_id"], ls_id,
                         (status.value, ordering_label))
        # Zero-write proof: exactly one row landed for this player.
        checker = SqlStore(self.url)
        try:
            rows = [m for m in checker.all_season_roster_memberships()
                   if m.player_id == player_id]
            self.assertEqual(len(rows), 1,
                             (status.value, ordering_label,
                              [r.id for r in rows]))
        finally:
            checker.close()

    def _assert_race_holds(self, status):
        self._assert_ordering(status, True, "team1_first")
        self._assert_ordering(status, False, "team2_first")

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
    the Season row lock, the new Player row lock, and migration 059's new
    index as backstop (#205 review round 1 finding 1) — for applicant/
    affiliate/inactive on DIFFERENT registered Teams of the SAME
    LeagueSeason, both orderings, exactly one success, and ZERO membership/
    event/audit rows for the loser (the required coverage the review
    names).

    #205 review round 2 (owner ruling + review finding 3) — REDESIGNED: two
    forced orderings per status via helpers.race_with_forced_order, gated
    on ``get_season_for_update`` — the FIRST lock BOTH calls contend for
    (both Teams register under the SAME LeagueSeason/Season, per
    _race_fixture below), held for the whole ``@_transactional``
    transaction, so forcing who acquires it first deterministically
    decides the whole race: the loser's own under-lock re-read, once its
    gated call unblocks, ALWAYS observes the winner's already-committed
    membership (real PostgreSQL FOR UPDATE semantics — see
    race_with_forced_order's docstring) — not 8 simultaneous-start rounds
    hoping both commit orders occurred.
    """

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]

    def _op(self, status, team_id, ls_id, player_id, name):
        def op(store):
            api = ApiService(store)
            return api.create_season_roster_membership(
                player_id, ls_id, team_id, status=status.value,
                jersey_number=None, reason=f"race-{name}",
                actor_id=f"actor_{name}")
        return op

    def _assert_ordering(self, status, team1_first, ordering_label):
        team1, team2, ls_id, player_id = _race_fixture(self.url)
        first_team, second_team = (
            (team1, team2) if team1_first else (team2, team1))
        first_res, second_res = race_with_forced_order(
            self.url, "get_season_for_update",
            self._op(status, first_team, ls_id, player_id,
                     f"{status.value}_{ordering_label}_first"),
            self._op(status, second_team, ls_id, player_id,
                     f"{status.value}_{ordering_label}_second"))
        self.assertNotIn("error", first_res,
                         (status.value, ordering_label, first_res))
        self.assertIn("error", second_res,
                      (status.value, ordering_label, second_res))
        self.assertEqual(second_res["error"]["details"]["reason"],
                         "membership_open_conflict",
                         (status.value, ordering_label))
        # Zero-write proof, service-level: exactly one membership, one
        # "created" event, one setup_audit_logs row for this player — the
        # loser's @_transactional rollback left NOTHING behind.
        checker = SqlStore(self.url)
        try:
            members = [m for m in checker.all_season_roster_memberships()
                      if m.player_id == player_id]
            self.assertEqual(len(members), 1, (status.value, ordering_label))
            events = checker.events_for_membership(members[0].id)
            self.assertEqual([e.action for e in events], ["created"],
                             (status.value, ordering_label))
            audits = [a for a in checker.all_setup_audit()
                     if a.entity_type == "season_roster_membership"
                     and a.entity_id == members[0].id]
            self.assertEqual(len(audits), 1, (status.value, ordering_label))
        finally:
            checker.close()

    def _assert_race_holds(self, status):
        self._assert_ordering(status, True, "team1_first")
        self._assert_ordering(status, False, "team2_first")

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
        # #205 review round 2 (owner ruling, overriding round 1 finding 5's
        # actor_id+reason floor): set_season_roster_membership_status now
        # refuses EVERY terminal transition unconditionally — proven here
        # on real PostgreSQL too, zero write, before the terminal state
        # this test still needs (to exercise "already terminal, immutable"
        # below) is constructed directly at the store layer instead, per
        # the owner ruling's explicit guidance for existing Slice-A
        # coverage that needs a terminal PRECONDITION.
        before_events = len(
            api.list_season_roster_membership_events(m["id"])["events"])
        before_audit = len(api.store.all_setup_audit())
        refused = api.set_season_roster_membership_status(
            m["id"], "released", reason="cut", actor_id=ADMIN)
        self.assertEqual(refused["error"]["code"], "forbidden")
        self.assertEqual(refused["error"]["details"]["reason"],
                         "terminal_transition_not_authorized")
        self.assertEqual(
            len(api.list_season_roster_membership_events(m["id"])["events"]),
            before_events)
        self.assertEqual(len(api.store.all_setup_audit()), before_audit)
        rel = end_membership_directly(api.store, m["id"], "released")
        self.assertIs(rel.status, MembershipStatus.RELEASED)
        frozen = api.set_season_roster_membership_status(
            m["id"], "active", actor_id=ADMIN)
        self.assertEqual(frozen["error"]["details"]["reason"],
                         "membership_terminal")
        # 2 events, not 3: "created" + "attributes_changed" (the update
        # above). The direct-store release above appends no event (it
        # bypasses the service, so there is no "status_changed" — see
        # end_membership_directly's docstring), unlike a real transition.
        events = api.list_season_roster_membership_events(m["id"])["events"]
        self.assertEqual([e["action"] for e in events],
                         ["created", "attributes_changed"])
        # Durable across a second connection to the same database.
        second = SqlStore(os.environ["TEST_DATABASE_URL"])
        try:
            row = second.get_season_roster_membership(m["id"])
            self.assertIs(row.status, MembershipStatus.RELEASED)
            self.assertEqual(row.jersey_number, 12)
            self.assertEqual(len(second.events_for_membership(m["id"])), 2)
        finally:
            second.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
