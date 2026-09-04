"""Cross-team substitute availability within one LeagueSeason (#287).

This is the production boundary behind the Player Home affordance "Games you
can sub in".  A player may volunteer for a *different* team's future game
before that team has an open slot, but only when the player's current seasonal
membership and the target side's registration belong to the exact same
LeagueSeason and the same non-null Division.  The target must be one of the
game's two sides and the player's own team must not be playing in that game.

The persisted enrollment belongs to ``(game, target_team)`` while its position
comes from the player's source SeasonRosterMembership.  Every later transition
revalidates both sides of that relationship: ending the source membership or
deactivating either registration cannot leave a stale candidate offerable or
acceptable.  Memory, SQLite and (when ``TEST_DATABASE_URL`` is configured)
real PostgreSQL run the identical contract; the harness proves the selected
backend so a configured PostgreSQL leg cannot silently fall back to SQLite.
"""

import copy
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import FakeClock, end_membership_directly, fresh_sql_store

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import AuditAction, Role
from hockey_scheduler.domain.enums import NotificationType
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv


ADMIN = "setup_admin"
UTC = timezone.utc
_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL); the #287 cross-team "
    "substitute contract was NOT exercised on PostgreSQL. A SKIP IS NOT A "
    "PASS: source/target membership reads and lifecycle writes must round-trip "
    "through the real PostgreSQL schema.")


def _ok(result):
    assert "error" not in result, result
    return result


def _save_registration(store, registration):
    if isinstance(store, SqlStore):
        with store.transaction():
            store.save_season_team_registration(registration)
    else:
        store.save_season_team_registration(registration)


def _save_game(store, game):
    if isinstance(store, SqlStore):
        with store.transaction():
            store.save_game(game)
    else:
        store.save_game(game)


def _save_player(store, player):
    if isinstance(store, SqlStore):
        with store.transaction():
            store.save_player(player)
    else:
        store.save_player(player)


def _save_substitute(store, enrollment):
    if isinstance(store, SqlStore):
        with store.transaction():
            store.save_substitute(enrollment)
    else:
        store.save_substitute(enrollment)


class _Fixture:
    """A Bronze source team and a Team 4-v-Team 5 target game.

    Team 1, Team 4 and Team 5 share one exact LeagueSeason and Division.
    The game starts with Team 4's only skater slot filled, proving that
    cross-team enrollment records willingness rather than an already-open
    vacancy.  Additional players cover every boundary the policy refuses.
    """

    def build(self, store):
        api = ApiService(store)
        api.roster.clock = FakeClock()

        org = _ok(api.create_organization("Org", "O", actor_id=ADMIN))
        program = _ok(api.create_program(
            "Program", operator_organization_id=org["id"], actor_id=ADMIN))
        season = _ok(api.create_season(
            program["id"], "2026", actor_id=ADMIN))
        league = _ok(api.create_league(
            season["id"], "Bronze", actor_id=ADMIN))
        bronze = _ok(api.create_division(
            season["id"], "Bronze", league_id=league["id"], actor_id=ADMIN))
        silver = _ok(api.create_division(
            season["id"], "Silver", league_id=league["id"], actor_id=ADMIN))
        club = _ok(api.create_club("Club", actor_id=ADMIN))

        def team(name, division_id=bronze["id"], *, league_id=league["id"]):
            row = _ok(api.create_team(
                club["id"], None, name, actor_id=ADMIN,
                program_id=program["id"], league_id=league_id))
            reg = _ok(api.register_team_for_season(
                season["id"], row["id"], division_id, actor_id=ADMIN,
                league_id=league_id))
            return row, reg

        team1, reg1 = team("Bronze Team 1")
        team4, reg4 = team("Bronze Team 4")
        team5, reg5 = team("Bronze Team 5")
        team6, reg6 = team("Silver Team 6", silver["id"])
        nodiv, reg_nodiv = team("Unplaced Team", None)

        player = _ok(api.create_player(
            team1["id"], "Bronze One Player", "goalie", actor_id=ADMIN))
        starter = _ok(api.create_player(
            team4["id"], "Team Four Starter", "defense", actor_id=ADMIN))
        silver_player = _ok(api.create_player(
            team6["id"], "Silver Player", "defense", actor_id=ADMIN))
        nodiv_player = _ok(api.create_player(
            nodiv["id"], "Unplaced Player", "defense", actor_id=ADMIN))

        # Same permanent League but a sibling LeagueSeason (a later Season).
        season2 = _ok(api.create_season(
            program["id"], "2027", actor_id=ADMIN))
        bronze2 = _ok(api.create_division(
            season2["id"], "Bronze", league_id=league["id"], actor_id=ADMIN))
        sibling_team = _ok(api.create_team(
            club["id"], None, "Sibling Season Team", actor_id=ADMIN,
            program_id=program["id"], league_id=league["id"]))
        _ok(api.register_team_for_season(
            season2["id"], sibling_team["id"], bronze2["id"],
            actor_id=ADMIN, league_id=league["id"]))
        sibling_player = _ok(api.create_player(
            sibling_team["id"], "Sibling Season Player", "defense",
            actor_id=ADMIN))

        # A different League in the current Season is also a different exact
        # LeagueSeason and must not project into Bronze.
        other_league = _ok(api.create_league(
            season["id"], "Gold", actor_id=ADMIN))
        gold = _ok(api.create_division(
            season["id"], "Gold", league_id=other_league["id"],
            actor_id=ADMIN))
        other_team = _ok(api.create_team(
            club["id"], None, "Gold Team", actor_id=ADMIN,
            program_id=program["id"], league_id=other_league["id"]))
        _ok(api.register_team_for_season(
            season["id"], other_team["id"], gold["id"], actor_id=ADMIN,
            league_id=other_league["id"]))
        other_player = _ok(api.create_player(
            other_team["id"], "Gold Player", "defense", actor_id=ADMIN))

        venue = _ok(api.create_venue(
            "Arena", organization_id=org["id"], league_id=program["id"],
            actor_id=ADMIN))
        _ok(api.grant_season_venue_access(
            season["id"], venue["id"], actor_id=ADMIN))
        rink = _ok(api.create_rink(venue["id"], "Rink 1", actor_id=ADMIN))

        def game(game_name, home_id, away_id, hour):
            start = datetime(2026, 2, 1, hour, tzinfo=UTC)
            slot = _ok(api.create_ice_slot(
                rink["id"], start.isoformat(),
                (start + timedelta(hours=1)).isoformat(), actor_id=ADMIN))
            row = _ok(api.create_game(
                season["id"], bronze["id"], home_id, away_id, slot["id"],
                target_goalies=1, target_skaters=1, actor_id=ADMIN,
                league_id=league["id"]))
            _ok(api.publish_game(row["id"], actor_id=ADMIN))
            return row

        target_game = game("target", team4["id"], team5["id"], 18)
        own_game = game("own", team1["id"], team4["id"], 20)
        _ok(api.select_roster(target_game["id"], [starter["id"]],
                              actor_id=ADMIN))

        memberships = api.list_season_roster_memberships(
            player_id=player["id"])["memberships"]
        (source_membership,) = [m for m in memberships
                                if m["league_season_id"]
                                == target_game["league_season_id"]]
        # Make the season-scoped position observably different from the
        # permanent Player pointer.  Cross-team projection must read this
        # membership (DEFENSE -> skater), never Player.position (GOALIE).
        _ok(api.update_season_roster_membership(
            source_membership["id"], position="defense",
            reason="season position", actor_id=ADMIN))

        return {
            "api": api,
            "store": store,
            "game": target_game,
            "own_game": own_game,
            "player": player,
            "starter": starter,
            "silver_player": silver_player,
            "nodiv_player": nodiv_player,
            "sibling_player": sibling_player,
            "other_player": other_player,
            "team1": team1,
            "team4": team4,
            "team5": team5,
            "reg1": reg1,
            "reg4": reg4,
            "reg5": reg5,
            "reg6": reg6,
            "reg_nodiv": reg_nodiv,
            "sibling_division": bronze2,
            "rink": rink,
            "source_membership_id": source_membership["id"],
        }

    @staticmethod
    def open_team4_slot(fx):
        result = fx["api"].set_availability(
            fx["game"]["id"], fx["starter"]["id"], "unavailable",
            actor_id=fx["starter"]["id"])
        assert "error" not in result, result


class _CrossTeamContract(unittest.TestCase):
    maxDiff = None

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """Prove that a configured PostgreSQL leg did not silently fall back."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    @staticmethod
    def _close(store):
        close = getattr(store, "close", None)
        if close is not None:
            close()

    def _assert_ran(self, labels):
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        else:
            print("\n[cross-team substitution] " + _PG_SKIP)
        self.assertEqual(set(labels), expected, sorted(labels))

    def _each(self):
        ran = []
        for label, store in self._stores():
            self._assert_backend(label, store)
            try:
                yield label, _Fixture().build(store)
            finally:
                ran.append(label)
                self._close(store)
        self._assert_ran(ran)

    @staticmethod
    def _game_write_state(store, game_id):
        """Every durable surface a refused substitute command may touch."""
        return copy.deepcopy({
            "roster": store.roster_for_game(game_id),
            "substitutes": store.substitutes_for_game(game_id),
            "audit": store.audit_for_game(game_id),
            "notifications": store.notifications_for_game(game_id),
        })

    def _assert_refused_without_writes(self, fx, invoke, context):
        game_id = fx["game"]["id"]
        before = self._game_write_state(fx["store"], game_id)
        result = invoke()
        self.assertIn("error", result, (context, result))
        self.assertEqual(
            self._game_write_state(fx["store"], game_id), before,
            (context, result))
        return result

    def _assert_only_expiry_write(self, before, after, player_id, context,
                                  *, reason):
        """Expiry changes one enrollment and appends exactly one audit row."""
        self.assertEqual(after["roster"], before["roster"], context)
        self.assertEqual(after["notifications"], before["notifications"],
                         context)
        expected_substitutes = copy.deepcopy(before["substitutes"])
        expected_sub = next(
            row for row in expected_substitutes
            if row.player_id == player_id)
        expected_sub.status = type(expected_sub.status).EXPIRED
        self.assertEqual(after["substitutes"], expected_substitutes, context)
        self.assertEqual(after["audit"][:-1], before["audit"], context)
        self.assertEqual(len(after["audit"]), len(before["audit"]) + 1,
                         context)
        expiry = after["audit"][-1]
        self.assertEqual(expiry.action, AuditAction.SUBSTITUTE_EXPIRED,
                         context)
        self.assertEqual(expiry.subject_player_id, player_id, context)
        self.assertEqual(expiry.detail.get("reason"), reason, context)


class CrossTeamAvailabilityContract(_CrossTeamContract):
    def test_same_division_game_is_listed_before_a_vacancy_exists(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                status = fx["api"].roster.compute_roster_status(
                    fx["game"]["id"], fx["team4"]["id"])
                self.assertEqual(status.open_skater_slots, 0, label)

                rows = fx["api"].get_player_home(
                    fx["player"]["id"])["substitute_opportunities"]
                by_target = {r["target_team_id"]: r for r in rows
                             if r["game_id"] == fx["game"]["id"]}
                # The game-side selection is explicit: a single game can
                # legitimately offer two distinct target teams.
                self.assertEqual(
                    set(by_target), {fx["team4"]["id"], fx["team5"]["id"]},
                    label)
                row = by_target[fx["team4"]["id"]]
                self.assertEqual(row["team_name"], "Bronze Team 4", label)
                self.assertEqual(row["opponent_name"], "Bronze Team 5", label)
                self.assertEqual(row["position_needed"], "skater", label)
                self.assertTrue(row["cross_team"], label)

    def test_player_home_serializes_the_validated_target_snapshot_once(self):
        """A membership change after discovery cannot break the whole Home.

        The cross-team listing returns the exact source/target proof it used.
        Projection must consume that immutable proof rather than resolving the
        relationship a second time and turning a concurrent membership change
        into a 404 for the entire signed-in Player Home response.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                snapshot = fx["api"].roster.active_substitute_snapshot(
                    fx["player"]["id"])
                choices = (
                    fx["api"].roster.list_cross_team_substitute_opportunities(
                        fx["player"]["id"], snapshot))
                self.assertTrue(choices, label)
                self.assertTrue(all(choice.target is not None
                                    for choice in choices), label)

                with mock.patch.object(
                        fx["api"].roster,
                        "list_cross_team_substitute_opportunities",
                        return_value=choices), mock.patch.object(
                            fx["api"].roster,
                            "resolve_substitute_target_context",
                            side_effect=AssertionError(
                                "Player Home re-resolved a validated target")):
                    home = fx["api"].get_player_home(fx["player"]["id"])

                rows = [
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]
                    and row.get("cross_team")]
                self.assertEqual(
                    {row["target_team_id"] for row in rows},
                    {fx["team4"]["id"], fx["team5"]["id"]}, label)
                for row in rows:
                    self.assertNotIn("source_membership_id", row, row)
                    self.assertNotIn("source_team_id", row, row)
                    self.assertNotIn("player_id", row, row)

    def test_enrollment_belongs_to_target_and_keeps_source_position(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                result = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertNotIn("error", result, result)
                self.assertNotIn("source_membership_id", result, result)
                self.assertNotIn("source_team_id", result, result)
                self.assertEqual(result["team_id"], fx["team4"]["id"], label)
                self.assertEqual(result["slot_type"], "skater", label)

                stored = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(stored.team_id, fx["team4"]["id"], label)
                self.assertEqual(
                    stored.source_membership_id,
                    fx["source_membership_id"], label)
                self.assertEqual(
                    stored.source_team_id, fx["team1"]["id"], label)
                public_list = fx["api"].get_substitutes(fx["game"]["id"])
                public_row = next(r for r in public_list
                                  if r["player_id"] == fx["player"]["id"])
                self.assertNotIn("source_membership_id", public_row, public_row)
                self.assertNotIn("source_team_id", public_row, public_row)
                self.assertEqual(
                    fx["store"].get_player(fx["player"]["id"]).position.value,
                    "goalie", label)
                # The seasonal membership is DEFENSE; it maps to the skater
                # slot and must win over the permanent GOALIE pointer.
                self.assertEqual(stored.slot_type.value, "skater", label)

                wrong_target = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team5"]["id"])
                self.assertEqual(
                    wrong_target.get("error", {}).get("code"),
                    "not_found", wrong_target)

                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertTrue(detail["can_withdraw"], detail)
                self.assertEqual(
                    detail["target_team_id"], fx["team4"]["id"], label)
                # A cross-team player may see the public fixture and their own
                # enrollment, never the target side's private shortage data.
                for private in ("roster_status", "team_status",
                                "open_goalie_slots", "open_skater_slots"):
                    self.assertNotIn(private, detail, (label, private, detail))

    def test_hidden_or_past_cross_team_detail_is_not_an_existence_oracle(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                original = fx["store"].get_game(fx["game"]["id"])
                cases = (
                    ("unpublished", False, False, False, False,
                     original.start_time, original.end_time),
                    ("draft", True, True, False, False,
                     original.start_time, original.end_time),
                    ("locked", True, False, True, False,
                     original.start_time, original.end_time),
                    ("cancelled", True, False, False, True,
                     original.start_time, original.end_time),
                    ("past", True, False, False, False,
                     datetime(2025, 12, 31, 18, tzinfo=UTC),
                     datetime(2025, 12, 31, 19, tzinfo=UTC)),
                )
                expected = {
                    "error": {"code": "not_found",
                              "message": "Opportunity not found."}}
                for (state, published, is_draft, locked, cancelled,
                     start, end) in cases:
                    with self.subTest(backend=label, state=state):
                        game = fx["store"].get_game(fx["game"]["id"])
                        game.published = published
                        game.is_draft = is_draft
                        game.locked = locked
                        game.cancelled = cancelled
                        game.start_time = start
                        game.end_time = end
                        _save_game(fx["store"], game)

                        result = fx["api"].get_substitute_opportunity(
                            fx["player"]["id"], fx["game"]["id"],
                            target_team_id=fx["team4"]["id"])
                        self.assertEqual(result, expected,
                                         (label, state, result))

    def test_hidden_cross_team_post_is_not_a_game_state_or_target_oracle(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                game = fx["store"].get_game(fx["game"]["id"])
                game.published = False
                game.is_draft = True
                game.locked = True
                _save_game(fx["store"], game)
                before_audit = list(
                    fx["store"].audit_for_game(fx["game"]["id"]))

                unknown = fx["api"].enroll_substitute(
                    "missing-cross-team-game", fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                valid_target = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                wrong_target = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team1"]["id"])

                expected = {
                    "error": {"code": "not_found",
                              "message": "Opportunity not found."}}
                self.assertEqual(unknown, expected, (label, unknown))
                self.assertEqual(valid_target, expected,
                                 (label, valid_target))
                self.assertEqual(wrong_target, expected,
                                 (label, wrong_target))
                self.assertIsNone(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]), label)
                self.assertEqual(
                    fx["store"].audit_for_game(fx["game"]["id"]),
                    before_audit, label)

    def test_existing_opt_in_remains_reachable_for_withdrawal_after_unpublish(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)

                game = fx["store"].get_game(fx["game"]["id"])
                game.published = False
                game.is_draft = True
                _save_game(fx["store"], game)

                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertTrue(detail["can_withdraw"], detail)
                self.assertFalse(detail["can_accept"], detail)

    def test_locked_or_cancelled_choice_stays_checked_but_cannot_withdraw(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)

                for state in ("locked", "cancelled"):
                    game = fx["store"].get_game(fx["game"]["id"])
                    game.locked = state == "locked"
                    game.cancelled = state == "cancelled"
                    _save_game(fx["store"], game)
                    rows = fx["api"].get_player_home(
                        fx["player"]["id"])["substitute_opportunities"]
                    (choice,) = [row for row in rows
                                 if row["game_id"] == fx["game"]["id"]]
                    self.assertEqual(
                        choice["enrollment_status"], "enrolled",
                        (label, state, choice))
                    self.assertFalse(
                        choice["can_withdraw"], (label, state, choice))
                    self.assertTrue(
                        choice["needs_cleanup"], (label, state, choice))
                    detail = fx["api"].get_substitute_opportunity(
                        fx["player"]["id"], fx["game"]["id"],
                        target_team_id=fx["team4"]["id"])
                    self.assertFalse(
                        detail["can_withdraw"], (label, state, detail))
                    self.assertTrue(
                        detail["needs_cleanup"], (label, state, detail))

    def test_inactive_player_opt_in_is_cleanup_only_on_every_surface(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                _Fixture.open_team4_slot(fx)
                player = fx["store"].get_player(fx["player"]["id"])
                player.is_active = False
                _save_player(fx["store"], player)

                home = fx["api"].get_player_home(fx["player"]["id"])
                (choice,) = [
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]
                    and row.get("target_team_id") == fx["team4"]["id"]]
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                cleanup = (
                    "Your eligibility changed; remove this saved "
                    "availability.")
                for surface, row in (("home", choice), ("detail", detail)):
                    self.assertTrue(row["needs_cleanup"],
                                    (label, surface, row))
                    self.assertTrue(row["can_withdraw"],
                                    (label, surface, row))
                    self.assertEqual(row["blocked_reason"], cleanup,
                                     (label, surface, row))
                    self.assertNotIn("source_membership_id", row, row)
                    self.assertNotIn("source_team_id", row, row)

                board = fx["api"].get_board(
                    fx["game"]["id"], fx["team4"]["id"],
                    viewer_role=Role.COACH)
                (lineup_row,) = [
                    row for row in board["players"]
                    if row["id"] == fx["player"]["id"]]
                self.assertEqual(lineup_row["group"], "substitute", lineup_row)
                self.assertEqual(lineup_row["sub_status"], "enrolled",
                                 lineup_row)
                self.assertFalse(lineup_row["eligible"], lineup_row)
                queue = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team4"]["id"])
                self.assertNotIn(
                    fx["player"]["id"],
                    [row["player_id"] for row in queue["candidates"]], queue)

                for command, invoke in (
                    ("offer", lambda: fx["api"].offer_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])),
                    ("override", lambda: fx["api"].add_substitute_to_roster(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])),
                ):
                    self._assert_refused_without_writes(
                        fx, invoke, (label, "inactive", command))

    def test_archived_canonical_season_makes_enrollment_cleanup_only(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                canonical_season_id = fx["game"]["season_id"]
                other_season_id = next(
                    season.id for season in fx["store"].all_seasons()
                    if season.id != canonical_season_id)
                game = fx["store"].get_game(fx["game"]["id"])
                game.season_id = other_season_id
                _save_game(fx["store"], game)
                fx["api"].setup.archive_season(
                    canonical_season_id, actor_id=ADMIN, reason="season over")

                home = fx["api"].get_player_home(fx["player"]["id"])
                (choice,) = [
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]
                    and row.get("target_team_id") == fx["team4"]["id"]]
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                for surface, row in (("home", choice), ("detail", detail)):
                    self.assertTrue(row["needs_cleanup"],
                                    (label, surface, row))
                    self.assertFalse(row["can_withdraw"],
                                     (label, surface, row))
                    self.assertIn("archived and read-only",
                                  row["blocked_reason"],
                                  (label, surface, row))

                board = fx["api"].get_board(
                    fx["game"]["id"], fx["team4"]["id"],
                    viewer_role=Role.COACH)
                (lineup_row,) = [
                    row for row in board["players"]
                    if row["id"] == fx["player"]["id"]]
                self.assertFalse(lineup_row["eligible"], lineup_row)
                queue = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team4"]["id"])
                self.assertNotIn(
                    fx["player"]["id"],
                    [row["player_id"] for row in queue["candidates"]], queue)
                self._assert_refused_without_writes(
                    fx,
                    lambda: fx["api"].withdraw_substitute(
                        fx["game"]["id"], fx["player"]["id"]),
                    (label, "archived", "withdraw"))

    def test_stale_source_opt_in_stays_visible_and_withdrawable(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "released")

                home = fx["api"].get_player_home(fx["player"]["id"])
                (choice,) = [
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]]
                self.assertEqual(choice["target_team_id"],
                                 fx["team4"]["id"], choice)
                self.assertEqual(choice["enrollment_status"],
                                 "enrolled", choice)
                self.assertTrue(choice["can_withdraw"], choice)
                self.assertFalse(choice["can_enroll"], choice)
                self.assertNotIn("source_membership_id", choice)
                self.assertNotIn("source_team_id", choice)

                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertTrue(detail["can_withdraw"], detail)
                self.assertTrue(detail["needs_cleanup"], detail)
                self.assertEqual(
                    detail["blocked_reason"],
                    "Your eligibility changed; remove this saved "
                    "availability.", detail)
                withdrawn = fx["api"].withdraw_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(withdrawn.get("status"), "withdrawn",
                                 withdrawn)

    def test_stale_target_registration_opt_in_stays_withdrawable(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                reg = fx["store"].get_season_team_registration(
                    fx["reg4"]["id"])
                reg.active = False
                _save_registration(fx["store"], reg)

                home = fx["api"].get_player_home(fx["player"]["id"])
                (choice,) = [
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]]
                self.assertEqual(choice["enrollment_status"],
                                 "enrolled", choice)
                self.assertTrue(choice["can_withdraw"], choice)
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertTrue(detail["can_withdraw"], detail)
                withdrawn = fx["api"].withdraw_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(withdrawn.get("status"), "withdrawn",
                                 withdrawn)

    def test_guardian_omits_fresh_cross_choices_but_keeps_cross_offer(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                with mock.patch.object(
                        fx["api"].guardians, "verified_junior_ids",
                        return_value=[fx["player"]["id"]]):
                    fresh = fx["api"].get_guardian_home("guardian")
                    (junior,) = fresh["juniors"]
                    self.assertFalse(
                        [row for row in junior["substitute_opportunities"]
                         if row.get("cross_team")], junior)

                    enrolled = fx["api"].enroll_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        target_team_id=fx["team4"]["id"])
                    self.assertEqual(enrolled.get("status"), "enrolled",
                                     enrolled)
                    _Fixture.open_team4_slot(fx)
                    offered = fx["api"].offer_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])
                    self.assertEqual(offered.get("status"), "offered",
                                     offered)

                    offered_home = fx["api"].get_guardian_home("guardian")
                    (junior,) = offered_home["juniors"]
                    (offer,) = junior["substitute_offers"]
                    self.assertTrue(offer["cross_team"], offer)
                    self.assertEqual(offer["target_team_id"],
                                     fx["team4"]["id"], offer)

    def test_source_provenance_is_not_published_in_target_coach_audit(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                board = fx["api"].get_board(
                    fx["game"]["id"], fx["team4"]["id"],
                    viewer_role=Role.COACH)
                events = [event for event in board["audit"]
                          if event["action"]
                          == AuditAction.SUBSTITUTE_ENROLLED.value]
                self.assertEqual(len(events), 1, (label, events))
                self.assertEqual(events[0]["detail"], {
                    "target_team_id": fx["team4"]["id"],
                    "cross_team": True,
                }, (label, events[0]))

    def test_accepted_borrowed_game_is_home_visible_until_player_backs_out(self):
        """An accepted Team 4 seat is a schedule fact, not private Team 4 data.

        The borrowing player must see the earlier game on Home and be able to
        use the ordinary ``Can't Play`` path, but the payload must not publish
        Team 4's aggregate roster status.  Once the durable seat stops
        occupying a slot, the borrowed game no longer belongs in their
        personal schedule or today's count.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                _Fixture.open_team4_slot(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)
                accepted = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(accepted.get("status"), "accepted", accepted)

                # Both fixtures are on 2026-02-01; the borrowed 18:00 game is
                # earlier than the player's own 20:00 game.
                fx["api"].roster.clock = lambda: datetime(
                    2026, 2, 1, 17, tzinfo=UTC)
                home = fx["api"].get_player_home(fx["player"]["id"])
                next_game = home["next_game"]
                self.assertEqual(next_game["game_id"], fx["game"]["id"], home)
                self.assertEqual(next_game["team_id"], fx["team4"]["id"], home)
                self.assertTrue(next_game["cross_team"], next_game)
                self.assertIsNone(next_game["team_status"], next_game)
                self.assertEqual(
                    next_game["attendance_status"], "confirmed", next_game)
                self.assertEqual(home["today_count"], 2, home)

                maybe = fx["api"].set_availability(
                    fx["game"]["id"], fx["player"]["id"], "maybe",
                    actor_id=fx["player"]["id"])
                self.assertNotIn("error", maybe, maybe)
                maybe_home = fx["api"].get_player_home(fx["player"]["id"])
                self.assertEqual(
                    maybe_home["next_game"]["attendance_status"], "pending",
                    (label, maybe_home))
                self.assertEqual(maybe_home["today_count"], 2, maybe_home)

                available = fx["api"].set_availability(
                    fx["game"]["id"], fx["player"]["id"], "available",
                    actor_id=fx["player"]["id"])
                self.assertNotIn("error", available, available)
                confirmed_entry = fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(
                    confirmed_entry.status.value, "confirmed",
                    (label, confirmed_entry))
                confirmed_home = fx["api"].get_player_home(
                    fx["player"]["id"])
                confirmed_game = confirmed_home["next_game"]
                self.assertEqual(
                    confirmed_game["game_id"], fx["game"]["id"],
                    confirmed_home)
                self.assertEqual(
                    confirmed_game["team_id"], fx["team4"]["id"],
                    confirmed_home)
                self.assertTrue(confirmed_game["cross_team"], confirmed_game)
                self.assertIsNone(
                    confirmed_game["team_status"], confirmed_game)
                self.assertEqual(
                    confirmed_game["attendance_status"], "confirmed",
                    confirmed_game)
                self.assertEqual(
                    confirmed_home["today_count"], 2, confirmed_home)

                unavailable = fx["api"].set_availability(
                    fx["game"]["id"], fx["player"]["id"], "unavailable",
                    actor_id=fx["player"]["id"])
                self.assertNotIn("error", unavailable, unavailable)
                entry = fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertFalse(entry.status.occupies_slot, (label, entry))

                after = fx["api"].get_player_home(fx["player"]["id"])
                self.assertEqual(
                    after["next_game"]["game_id"], fx["own_game"]["id"], after)
                self.assertEqual(after["today_count"], 1, after)

    def test_available_refuses_mismatched_borrowed_row_without_writes(self):
        """Corrupt enrollment identity cannot authorize its paired row.

        The accepted borrowed row and accepted enrollment are one durable
        authority only while their target side, slot, and source identity
        agree.  A mismatch must fail closed before availability, roster,
        audit, or notification state changes; the player's ordinary Team 1
        membership must not be treated as authority for either corrupt
        borrowed side.

        The source-identity case deliberately leaves the claimed source Team
        1 live and removes only the named membership.  It therefore catches a
        corrupt half-reference without forbidding the separate historical
        case where an explicit subtree deletion removed both source rows.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                gid = fx["game"]["id"]
                pid = fx["player"]["id"]
                enrolled = fx["api"].enroll_substitute(
                    gid, pid, target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                _Fixture.open_team4_slot(fx)
                offered = fx["api"].offer_substitute(
                    gid, pid, authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)
                accepted = fx["api"].accept_substitute(gid, pid)
                self.assertEqual(accepted.get("status"), "accepted", accepted)
                maybe = fx["api"].set_availability(
                    gid, pid, "maybe", actor_id=pid)
                self.assertNotIn("error", maybe, maybe)

                enrollment = fx["store"].substitute_for_player(gid, pid)
                enrollment.team_id = fx["team5"]["id"]
                _save_substitute(fx["store"], enrollment)
                before = {
                    "game": self._game_write_state(fx["store"], gid),
                    "availability": copy.deepcopy(
                        fx["store"].availability_for_player(gid, pid)),
                }

                refused = fx["api"].set_availability(
                    gid, pid, "available", actor_id=pid)
                self.assertEqual(
                    refused.get("error", {}).get("code"), "not_eligible",
                    (label, refused))
                self.assertEqual(
                    {
                        "game": self._game_write_state(fx["store"], gid),
                        "availability": copy.deepcopy(
                            fx["store"].availability_for_player(gid, pid)),
                    },
                    before,
                    (label, refused),
                )

                enrollment = fx["store"].substitute_for_player(gid, pid)
                enrollment.team_id = fx["team4"]["id"]
                enrollment.source_membership_id = "missing-source-membership"
                _save_substitute(fx["store"], enrollment)
                source_before = {
                    "game": self._game_write_state(fx["store"], gid),
                    "availability": copy.deepcopy(
                        fx["store"].availability_for_player(gid, pid)),
                }

                source_refused = fx["api"].set_availability(
                    gid, pid, "available", actor_id=pid)
                self.assertEqual(
                    source_refused.get("error", {}).get("code"),
                    "not_eligible", (label, source_refused))
                self.assertEqual(
                    {
                        "game": self._game_write_state(fx["store"], gid),
                        "availability": copy.deepcopy(
                            fx["store"].availability_for_player(gid, pid)),
                    },
                    source_before,
                    (label, source_refused),
                )

                # Model the post-#429 historical shape without coupling this
                # availability contract to the deletion service: both frozen
                # source ids remain on the enrollment, while neither source
                # row remains resolvable.  Paired absence is deliberate and
                # must not be mistaken for the corrupt half-reference above.
                enrollment = fx["store"].substitute_for_player(gid, pid)
                enrollment.source_team_id = "deleted-source-team"
                enrollment.source_membership_id = "deleted-source-membership"
                _save_substitute(fx["store"], enrollment)

                historical = fx["api"].set_availability(
                    gid, pid, "available", actor_id=pid)
                self.assertNotIn("error", historical, (label, historical))
                historical_entry = fx["store"].roster_entry_for_player(
                    gid, pid)
                self.assertEqual(
                    historical_entry.status.value, "confirmed",
                    (label, historical_entry))

    def test_terminal_team4_then_accepted_team5_keeps_each_activity_on_its_side(self):
        """Historical and current side claims must not erase each other.

        A withdrawn Team 4 lifecycle and an accepted Team 5 lifecycle name
        the same player and game.  Event-level side snapshots keep both
        histories visible to the coach that owns them without leaking either
        lifecycle to the other side.  The snapshot is internal metadata and
        must not change the board JSON shape.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                gid = fx["game"]["id"]
                pid = fx["player"]["id"]
                team4 = fx["team4"]["id"]
                team5 = fx["team5"]["id"]

                first = fx["api"].enroll_substitute(
                    gid, pid, target_team_id=team4)
                self.assertEqual(first.get("status"), "enrolled", first)
                withdrawn = fx["api"].withdraw_substitute(gid, pid)
                self.assertEqual(withdrawn.get("status"), "withdrawn", withdrawn)

                second = fx["api"].enroll_substitute(
                    gid, pid, target_team_id=team5)
                self.assertEqual(second.get("status"), "enrolled", second)
                offered = fx["api"].offer_substitute(
                    gid, pid, authorized_team_id=team5)
                self.assertEqual(offered.get("status"), "offered", offered)
                accepted = fx["api"].accept_substitute(gid, pid)
                self.assertEqual(accepted.get("status"), "accepted", accepted)

                boards = {
                    team4: fx["api"].get_board(
                        gid, team4, viewer_role=Role.COACH),
                    team5: fx["api"].get_board(
                        gid, team5, viewer_role=Role.COACH),
                    "operator": fx["api"].get_board(
                        gid, team4, viewer_role=Role.LEAGUE_ADMIN),
                }

                def related_audit(board):
                    return [row for row in board["audit"]
                            if row["subject_player_id"] == pid]

                def related_notifications(board):
                    return [row for row in board["notifications"]
                            if row["subject_player_id"] == pid]

                self.assertEqual(
                    [row["action"] for row in related_audit(boards[team4])],
                    [AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_WITHDRAWN.value],
                    (label, boards[team4]))
                self.assertEqual(
                    [row["type"]
                     for row in related_notifications(boards[team4])],
                    [NotificationType.SUBSTITUTE_ENROLLED.value],
                    (label, boards[team4]))
                self.assertEqual(
                    [row["action"] for row in related_audit(boards[team5])],
                    [AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_OFFERED.value,
                     AuditAction.SUBSTITUTE_ACCEPTED.value],
                    (label, boards[team5]))
                self.assertEqual(
                    [row["type"]
                     for row in related_notifications(boards[team5])],
                    [NotificationType.SUBSTITUTE_ENROLLED.value,
                     NotificationType.SUBSTITUTE_OFFERED.value,
                     NotificationType.SUBSTITUTE_ACCEPTED.value],
                    (label, boards[team5]))
                self.assertEqual(
                    [row["action"]
                     for row in related_audit(boards["operator"])],
                    [AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_WITHDRAWN.value,
                     AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_OFFERED.value,
                     AuditAction.SUBSTITUTE_ACCEPTED.value],
                    (label, boards["operator"]))
                self.assertEqual(
                    boards[team5]["audit_count"], len(boards[team5]["audit"]),
                    (label, boards[team5]))
                self.assertIn(
                    pid, [row["id"] for row in boards[team5]["players"]],
                    (label, boards[team5]))

                for board in boards.values():
                    for row in board["audit"] + board["notifications"]:
                        self.assertNotIn("team_id", row, (label, row))
                        self.assertNotIn("_team_id", row, (label, row))

                stored_audit = [
                    row for row in fx["store"].audit_for_game(gid)
                    if row.subject_player_id == pid]
                stored_notifications = [
                    row for row in fx["store"].notifications_for_game(gid)
                    if row.subject_player_id == pid]
                self.assertEqual(
                    [row.team_id for row in stored_audit],
                    [team4, team4, team5, team5, team5],
                    (label, stored_audit))
                self.assertEqual(
                    [row.team_id for row in stored_notifications],
                    [team4, team5, team5, team5],
                    (label, stored_notifications))

    def test_same_team_history_survives_transfer_then_cross_team_acceptance(self):
        """Same-team and cross-team event snapshots retain original sides."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                gid = fx["own_game"]["id"]
                pid = fx["player"]["id"]
                team1 = fx["team1"]["id"]
                team4 = fx["team4"]["id"]
                team5 = fx["team5"]["id"]

                same_team = fx["api"].enroll_substitute(gid, pid)
                self.assertEqual(same_team.get("status"), "enrolled", same_team)
                self.assertEqual(same_team.get("team_id"), team1, same_team)
                withdrawn = fx["api"].withdraw_substitute(gid, pid)
                self.assertEqual(withdrawn.get("status"), "withdrawn", withdrawn)

                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "transferred")
                replacement = fx["api"].create_season_roster_membership(
                    pid, fx["own_game"]["league_season_id"], team5,
                    position="defense", reason="moved before borrowing",
                    actor_id=ADMIN)
                self.assertNotIn("error", replacement, replacement)

                borrowed = fx["api"].enroll_substitute(
                    gid, pid, target_team_id=team4)
                self.assertEqual(borrowed.get("status"), "enrolled", borrowed)
                offered = fx["api"].offer_substitute(
                    gid, pid, authorized_team_id=team4)
                self.assertEqual(offered.get("status"), "offered", offered)
                accepted = fx["api"].accept_substitute(gid, pid)
                self.assertEqual(accepted.get("status"), "accepted", accepted)

                team1_board = fx["api"].get_board(
                    gid, team1, viewer_role=Role.COACH)
                team4_board = fx["api"].get_board(
                    gid, team4, viewer_role=Role.COACH)

                def activity(board, key):
                    section = "audit" if key == "action" else "notifications"
                    return [row[key] for row in board[section]
                            if row["subject_player_id"] == pid]

                self.assertEqual(
                    activity(team1_board, "action"),
                    [AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_WITHDRAWN.value],
                    (label, team1_board))
                self.assertEqual(
                    activity(team1_board, "type"),
                    [NotificationType.SUBSTITUTE_ENROLLED.value],
                    (label, team1_board))
                self.assertEqual(
                    activity(team4_board, "action"),
                    [AuditAction.SUBSTITUTE_ENROLLED.value,
                     AuditAction.SUBSTITUTE_OFFERED.value,
                     AuditAction.SUBSTITUTE_ACCEPTED.value],
                    (label, team4_board))
                self.assertEqual(
                    activity(team4_board, "type"),
                    [NotificationType.SUBSTITUTE_ENROLLED.value,
                     NotificationType.SUBSTITUTE_OFFERED.value,
                     NotificationType.SUBSTITUTE_ACCEPTED.value],
                    (label, team4_board))

                stored_audit = [
                    row for row in fx["store"].audit_for_game(gid)
                    if row.subject_player_id == pid]
                stored_notifications = [
                    row for row in fx["store"].notifications_for_game(gid)
                    if row.subject_player_id == pid]
                self.assertEqual(
                    [row.team_id for row in stored_audit],
                    [team1, team1, team4, team4, team4],
                    (label, stored_audit))
                self.assertEqual(
                    [row.team_id for row in stored_notifications],
                    [team1, team4, team4, team4],
                    (label, stored_notifications))
                for board in (team1_board, team4_board):
                    for row in board["audit"] + board["notifications"]:
                        self.assertNotIn("team_id", row, (label, row))
                        self.assertNotIn("_team_id", row, (label, row))

    def test_legacy_null_event_side_keeps_conservative_unique_side_fallback(self):
        """Pre-migration events remain readable only with one durable side."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                gid = fx["game"]["id"]
                pid = fx["player"]["id"]
                team4 = fx["team4"]["id"]
                team5 = fx["team5"]["id"]
                enrolled = fx["api"].enroll_substitute(
                    gid, pid, target_team_id=team4)
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)

                def write_legacy_events():
                    fx["api"].roster._audit(
                        gid, AuditAction.AVAILABILITY_SET,
                        subject_player_id=pid,
                        detail={"legacy_null_side": True})
                    fx["api"].roster._notify(
                        gid, NotificationType.PLAYER_BACKED_OUT,
                        audience="coach", message="legacy-null-side",
                        subject_player_id=pid)
                    fx["api"].roster._audit(
                        gid, AuditAction.AVAILABILITY_SET,
                        subject_player_id=pid,
                        detail={"corrupt_event_side": True},
                        team_id="not-a-game-side")
                    fx["api"].roster._notify(
                        gid, NotificationType.PLAYER_BACKED_OUT,
                        audience="coach", message="corrupt-event-side",
                        subject_player_id=pid, team_id="not-a-game-side")

                if isinstance(fx["store"], SqlStore):
                    with fx["store"].transaction():
                        write_legacy_events()
                else:
                    write_legacy_events()

                own = fx["api"].get_board(
                    gid, team4, viewer_role=Role.COACH)
                other = fx["api"].get_board(
                    gid, team5, viewer_role=Role.COACH)
                operator = fx["api"].get_board(
                    gid, team4, viewer_role=Role.LEAGUE_ADMIN)
                self.assertEqual(
                    len([row for row in own["audit"]
                         if row["detail"].get("legacy_null_side")]), 1,
                    (label, own))
                self.assertEqual(
                    len([row for row in own["notifications"]
                         if row["message"] == "legacy-null-side"]), 1,
                    (label, own))
                self.assertFalse(
                    [row for row in other["audit"]
                     if row["detail"].get("legacy_null_side")],
                    (label, other))
                self.assertFalse(
                    [row for row in other["notifications"]
                     if row["message"] == "legacy-null-side"],
                    (label, other))
                for board in (own, other):
                    self.assertFalse(
                        [row for row in board["audit"]
                         if row["detail"].get("corrupt_event_side")],
                        (label, board))
                    self.assertFalse(
                        [row for row in board["notifications"]
                         if row["message"] == "corrupt-event-side"],
                        (label, board))
                self.assertEqual(
                    len([row for row in operator["audit"]
                         if row["detail"].get("corrupt_event_side")]), 1,
                    (label, operator))
                self.assertEqual(
                    len([row for row in operator["notifications"]
                         if row["message"] == "corrupt-event-side"]), 1,
                    (label, operator))
                for row in operator["audit"] + operator["notifications"]:
                    self.assertNotIn("team_id", row, (label, row))
                    self.assertNotIn("_team_id", row, (label, row))

    def test_active_opt_in_wins_over_terminal_history_in_lineup(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                for _ in range(9):
                    result = fx["api"].enroll_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        target_team_id=fx["team4"]["id"])
                    self.assertNotIn("error", result, result)
                    withdrawn = fx["api"].withdraw_substitute(
                        fx["game"]["id"], fx["player"]["id"])
                    self.assertEqual(withdrawn.get("status"), "withdrawn")
                final = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(final.get("status"), "enrolled", final)

                rows = fx["api"].roster.lineup_population(
                    fx["store"].get_game(fx["game"]["id"]),
                    fx["team4"]["id"])
                (row,) = [candidate for candidate in rows
                          if candidate.player.id == fx["player"]["id"]]
                active = fx["api"].roster._active_substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(row.source, "substitute", (label, row))
                self.assertEqual(row.enrollment.id, active.id, (label, row))
                status = fx["api"].roster.compute_roster_status(
                    fx["game"]["id"], fx["team4"]["id"])
                self.assertEqual(status.substitutes_enrolled, 1, label)


class CrossTeamBoundaryContract(_CrossTeamContract):
    def _assert_refused(self, fx, player_id, game_id, target_id, label):
        result = fx["api"].enroll_substitute(
            game_id, player_id, target_team_id=target_id)
        self.assertIn("error", result, (label, result))
        self.assertIsNone(
            fx["store"].substitute_for_player(game_id, player_id), label)

    def test_boundary_matrix_fails_closed(self):
        cases = (
            ("cross_division", "silver_player", "game", "team4"),
            ("missing_division", "nodiv_player", "game", "team4"),
            ("sibling_league_season", "sibling_player", "game", "team4"),
            ("cross_league", "other_player", "game", "team4"),
            ("target_not_a_game_side", "player", "game", "team1"),
            ("source_team_is_playing", "player", "own_game", "team4"),
        )
        ran = []
        for backend, store in self._stores():
            self._assert_backend(backend, store)
            try:
                for name, player_key, game_key, target_key in cases:
                    with self.subTest(backend=backend, case=name):
                        fx = _Fixture().build(store)
                        self._assert_refused(
                            fx, fx[player_key]["id"], fx[game_key]["id"],
                            fx[target_key]["id"], f"{backend}/{name}")
                        # Rebuild a clean SQL store for the next case.
                        if isinstance(store, SqlStore):
                            store.clear_all_data()
            finally:
                ran.append(backend)
                self._close(store)
        self._assert_ran(ran)

    def test_target_registration_without_division_is_refused(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                reg = fx["store"].get_season_team_registration(
                    fx["reg4"]["id"])
                reg.division_id = None
                _save_registration(fx["store"], reg)
                self._assert_refused(
                    fx, fx["player"]["id"], fx["game"]["id"],
                    fx["team4"]["id"], label)

    def test_dangling_shared_division_id_is_refused(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                for key in ("reg1", "reg4"):
                    reg = fx["store"].get_season_team_registration(
                        fx[key]["id"])
                    reg.division_id = "ghost-division"
                    _save_registration(fx["store"], reg)
                self._assert_refused(
                    fx, fx["player"]["id"], fx["game"]["id"],
                    fx["team4"]["id"], label)

    def test_sibling_league_season_division_id_is_refused(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                sibling = fx["sibling_division"]["id"]
                for key in ("reg1", "reg4"):
                    reg = fx["store"].get_season_team_registration(
                        fx[key]["id"])
                    reg.division_id = sibling
                    _save_registration(fx["store"], reg)
                self._assert_refused(
                    fx, fx["player"]["id"], fx["game"]["id"],
                    fx["team4"]["id"], label)

    def test_cross_team_target_must_be_explicit(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                result = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", result, result)
                self.assertIsNone(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]), label)

    def test_half_written_provenance_cannot_use_scoped_player_cleanup(self):
        """Either half of the durable source pair opts into fail-closed mode.

        Withdrawal deliberately tolerates a later-invalid source membership,
        so this is the response path on which deleting both pair-shape guards
        would otherwise turn corrupt cross-team history into a valid write.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
                original = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                pair = {
                    "source_membership_id": original.source_membership_id,
                    "source_team_id": original.source_team_id,
                }

                for missing in pair:
                    with self.subTest(backend=label, missing=missing):
                        row = fx["store"].substitute_for_player(
                            fx["game"]["id"], fx["player"]["id"])
                        row.source_membership_id = pair["source_membership_id"]
                        row.source_team_id = pair["source_team_id"]
                        setattr(row, missing, None)
                        _save_substitute(fx["store"], row)
                        before = self._game_write_state(
                            fx["store"], fx["game"]["id"])

                        refused = fx["api"].withdraw_substitute(
                            fx["game"]["id"], fx["player"]["id"],
                            expected_target_team_id=fx["team4"]["id"],
                            require_target_identity=True)
                        self.assertEqual(
                            refused.get("error", {}).get("code"),
                            "invalid_transition", (label, missing, refused))
                        self.assertEqual(
                            self._game_write_state(
                                fx["store"], fx["game"]["id"]),
                            before, (label, missing, refused))

                restored = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                restored.source_membership_id = pair["source_membership_id"]
                restored.source_team_id = pair["source_team_id"]
                _save_substitute(fx["store"], restored)


class CrossTeamOutreachContract(_CrossTeamContract):
    def _enroll_and_open(self, fx):
        enrolled = fx["api"].enroll_substitute(
            fx["game"]["id"], fx["player"]["id"],
            target_team_id=fx["team4"]["id"])
        self.assertNotIn("error", enrolled, enrolled)
        _Fixture.open_team4_slot(fx)

    @staticmethod
    def _advance_clock_when_player_locks(fx, before_lock, after_lock):
        """Return a patch that makes the Player lock cross a time boundary."""
        now = {"value": before_lock}
        original = fx["store"].get_player_for_update
        fx["api"].roster.clock = lambda: now["value"]

        def lock_then_advance(player_id):
            player = original(player_id)
            if player_id == fx["player"]["id"]:
                now["value"] = after_lock
            return player

        return mock.patch.object(
            fx["store"], "get_player_for_update",
            side_effect=lock_then_advance)

    def test_puck_drop_excludes_every_cross_team_forward_transition(self):
        """The #287 window is half-open: at game_start, expiry wins."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                before_drop = drop - timedelta(microseconds=1)

                # At the boundary a fresh choice is neither advertised nor
                # writable, and the refusal creates no enrollment/audit/feed
                # residue.
                fx["api"].roster.clock = lambda: drop
                home = fx["api"].get_player_home(fx["player"]["id"])
                self.assertFalse([
                    row for row in home["substitute_opportunities"]
                    if row["game_id"] == fx["game"]["id"]
                    and row.get("target_team_id") == fx["team4"]["id"]
                ], (label, home))
                self._assert_refused_without_writes(
                    fx,
                    lambda: fx["api"].enroll_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        target_team_id=fx["team4"]["id"]),
                    (label, "enroll-at-drop"))

                # One tick before remains valid. Once the clock reaches the
                # boundary, neither coach forward path may write.
                fx["api"].roster.clock = lambda: before_drop
                self._enroll_and_open(fx)
                fx["api"].roster.clock = lambda: drop
                for command, invoke in (
                    ("offer", lambda: fx["api"].offer_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])),
                    ("override", lambda: fx["api"].add_substitute_to_roster(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])),
                ):
                    self._assert_refused_without_writes(
                        fx, invoke, (label, command + "-at-drop"))

                # An offer legitimately created one tick before puck drop is
                # still not acceptable at equality, and remains unchanged.
                fx["api"].roster.clock = lambda: before_drop
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                self.assertEqual(offered["offered_at"], before_drop.isoformat(),
                                 (label, offered))
                self.assertEqual(offered["offer_expires_at"], drop.isoformat(),
                                 (label, offered))
                fx["api"].roster.clock = lambda: drop
                before = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                result = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", result, (label, result))
                self.assertIn("expired", result["error"]["message"].lower(),
                              (label, result))
                after = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                self._assert_only_expiry_write(
                    before, after, fx["player"]["id"],
                    (label, result, after), reason="accept_after_deadline")

    def test_cross_team_offer_uses_server_owned_response_window(self):
        """The body cannot choose expiry; default is min(now + 30m, drop)."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                now = drop - timedelta(hours=1)
                fx["api"].roster.clock = lambda: now
                self._assert_refused_without_writes(
                    fx,
                    lambda: fx["api"].offer_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        expires_at=(drop + timedelta(days=1)).isoformat(),
                        authorized_team_id=fx["team4"]["id"]),
                    (label, "request-controlled-expiry"))
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                self.assertEqual(offered["offered_at"], now.isoformat(),
                                 (label, offered))
                self.assertEqual(
                    offered["offer_expires_at"],
                    (now + timedelta(minutes=30)).isoformat(),
                                 (label, offered))

    def test_cross_team_offer_window_is_trusted_configuration(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                now = drop - timedelta(hours=1)
                fx["api"].roster.clock = lambda: now
                fx["api"].roster.cross_team_response_window = timedelta(
                    minutes=5)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                self.assertEqual(
                    offered["offer_expires_at"],
                    (now + timedelta(minutes=5)).isoformat(),
                    (label, offered))

    def test_player_home_offer_projection_survives_terminal_read_race(self):
        """A second lookup must not turn an observed offer into a 500."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))

                original = fx["store"].substitute_enrollments_for_player
                raced = {"done": False}

                def terminal_after_observation(player_id):
                    rows = original(player_id)
                    row = next((candidate for candidate in rows
                                if candidate.game_id == fx["game"]["id"]
                                and candidate.status.is_active_enrollment),
                               None)
                    if not raced["done"] and row is not None:
                        raced["done"] = True
                        observed_rows = copy.deepcopy(rows)
                        terminal = copy.deepcopy(row)
                        terminal.status = type(terminal.status).DECLINED
                        with fx["store"].transaction():
                            fx["store"].save_substitute(terminal)
                        return observed_rows
                    return rows

                with mock.patch.object(
                        fx["store"], "substitute_enrollments_for_player",
                        side_effect=terminal_after_observation):
                    home = fx["api"].get_player_home(fx["player"]["id"])
                self.assertNotIn("error", home, (label, home))
                self.assertEqual(
                    [row["game_id"] for row in home["substitute_offers"]],
                    [fx["game"]["id"]], (label, home))
                self.assertEqual(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]).status.value,
                    "declined", label)

    def test_player_home_uses_one_empty_snapshot_during_offer_race(self):
        """A later offer cannot partially suppress the observed target axis."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))

                original = fx["store"].substitute_enrollments_for_player
                reads = {"count": 0}

                def empty_then_current(player_id):
                    reads["count"] += 1
                    return [] if reads["count"] == 1 else original(player_id)

                with mock.patch.object(
                        fx["store"], "substitute_enrollments_for_player",
                        side_effect=empty_then_current):
                    home = fx["api"].get_player_home(fx["player"]["id"])
                self.assertEqual(home["substitute_offers"], [], (label, home))
                self.assertEqual(
                    {row["target_team_id"]
                     for row in home["substitute_opportunities"]
                     if row["game_id"] == fx["game"]["id"]
                     and row.get("cross_team")},
                    {fx["team4"]["id"], fx["team5"]["id"]},
                    (label, home))
                self.assertEqual(reads["count"], 1, (label, reads))
                self.assertEqual(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]).status.value,
                    "offered", label)

    def test_enrollment_samples_deadline_after_player_lock(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                with self._advance_clock_when_player_locks(
                        fx, drop - timedelta(microseconds=1), drop):
                    self._assert_refused_without_writes(
                        fx,
                        lambda: fx["api"].enroll_substitute(
                            fx["game"]["id"], fx["player"]["id"],
                            target_team_id=fx["team4"]["id"]),
                        (label, "enroll-straddled-player-lock"))

    def test_offer_samples_deadline_after_player_lock(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                with self._advance_clock_when_player_locks(
                        fx, drop - timedelta(microseconds=1), drop):
                    self._assert_refused_without_writes(
                        fx,
                        lambda: fx["api"].offer_substitute(
                            fx["game"]["id"], fx["player"]["id"],
                            authorized_team_id=fx["team4"]["id"]),
                        (label, "offer-straddled-player-lock"))

    def test_override_samples_deadline_after_player_lock(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                with self._advance_clock_when_player_locks(
                        fx, drop - timedelta(microseconds=1), drop):
                    self._assert_refused_without_writes(
                        fx,
                        lambda: fx["api"].add_substitute_to_roster(
                            fx["game"]["id"], fx["player"]["id"],
                            authorized_team_id=fx["team4"]["id"]),
                        (label, "override-straddled-player-lock"))

    def test_accept_samples_deadline_after_player_lock(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                before_drop = drop - timedelta(microseconds=1)
                fx["api"].roster.clock = lambda: before_drop
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                before = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                with self._advance_clock_when_player_locks(
                        fx, before_drop, drop):
                    result = fx["api"].accept_substitute(
                        fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", result, (label, result))
                self.assertIn("expired", result["error"]["message"].lower(),
                              (label, result))
                after = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                self._assert_only_expiry_write(
                    before, after, fx["player"]["id"],
                    (label, result, after), reason="accept_after_deadline")

    def _assert_cross_team_decline_expires_at(self, offset):
        for label, fx in self._each():
            with self.subTest(backend=label, offset=offset):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                issued_at = drop - timedelta(hours=1)
                fx["api"].roster.clock = lambda: issued_at
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                deadline = datetime.fromisoformat(offered["offer_expires_at"])
                fx["api"].roster.clock = lambda: deadline + offset

                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertTrue(detail["can_decline_offer"], (label, detail))
                self.assertTrue(detail["offer_expired"], (label, detail))
                self.assertIn("expired", detail["blocked_reason"].lower(),
                              (label, detail))

                before = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                result = fx["api"].decline_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(result.get("status"), "expired",
                                 (label, result))
                after = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                self._assert_only_expiry_write(
                    before, after, fx["player"]["id"],
                    (label, result, after), reason="decline_after_deadline")

                # Retrying a terminal response cannot append a second expiry
                # fact or mutate any other surface.
                retry = fx["api"].decline_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", retry, (label, retry))
                self.assertEqual(
                    self._game_write_state(fx["store"], fx["game"]["id"]),
                    after, (label, retry))

                # The terminal row releases the active uniqueness slot so a
                # later eligible lifecycle can begin cleanly.
                fx["api"].roster.clock = lambda: issued_at
                reenrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(reenrolled.get("status"), "enrolled",
                                 (label, reenrolled))

    def test_cross_team_decline_at_deadline_records_only_expiry(self):
        self._assert_cross_team_decline_expires_at(timedelta(0))

    def test_cross_team_decline_after_deadline_records_only_expiry(self):
        self._assert_cross_team_decline_expires_at(
            timedelta(microseconds=1))

    def test_cross_team_offered_withdraw_cannot_choose_terminal_state(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                issued_at = drop - timedelta(hours=1)
                fx["api"].roster.clock = lambda: issued_at
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)

                before = self._game_write_state(fx["store"], fx["game"]["id"])
                refused = fx["api"].withdraw_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", refused, (label, refused))
                self.assertIn("accept or decline",
                              refused["error"]["message"].lower())
                self.assertEqual(
                    self._game_write_state(fx["store"], fx["game"]["id"]),
                    before, (label, refused))

                deadline = datetime.fromisoformat(
                    offered["offer_expires_at"])
                fx["api"].roster.clock = lambda: deadline
                before_expiry = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                expired = fx["api"].withdraw_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(expired.get("status"), "expired",
                                 (label, expired))
                self._assert_only_expiry_write(
                    before_expiry,
                    self._game_write_state(fx["store"], fx["game"]["id"]),
                    fx["player"]["id"], (label, expired),
                    reason="withdraw_after_deadline")

    def _assert_cross_team_override_expires_at(self, offset):
        for label, fx in self._each():
            with self.subTest(backend=label, offset=offset):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                issued_at = drop - timedelta(hours=1)
                fx["api"].roster.clock = lambda: issued_at
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)
                deadline = datetime.fromisoformat(
                    offered["offer_expires_at"])
                fx["api"].roster.clock = lambda: deadline + offset
                before = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                result = fx["api"].add_substitute_to_roster(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertIn("error", result, (label, result))
                self.assertIn("expired", result["error"]["message"].lower(),
                              (label, result))
                self._assert_only_expiry_write(
                    before,
                    self._game_write_state(fx["store"], fx["game"]["id"]),
                    fx["player"]["id"], (label, result),
                    reason="override_after_deadline")

    def test_cross_team_override_at_deadline_records_only_expiry(self):
        self._assert_cross_team_override_expires_at(timedelta(0))

    def test_cross_team_override_after_deadline_records_only_expiry(self):
        self._assert_cross_team_override_expires_at(
            timedelta(microseconds=1))

    def test_move_to_earlier_start_expires_existing_offer_at_new_drop(self):
        """A reschedule shortens an issued offer; it never revives it."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                original_drop = fx["store"].get_game(
                    fx["game"]["id"]).start_time
                issued_at = original_drop - timedelta(minutes=75)
                fx["api"].roster.clock = lambda: issued_at
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                self.assertEqual(
                    offered["offer_expires_at"],
                    (issued_at + timedelta(minutes=30)).isoformat(),
                    (label, offered))

                earlier_drop = original_drop - timedelta(hours=1)
                new_slot = _ok(fx["api"].create_ice_slot(
                    fx["rink"]["id"], earlier_drop.isoformat(),
                    (earlier_drop + timedelta(hours=1)).isoformat(),
                    actor_id=ADMIN))
                moved = fx["api"].move_game(
                    fx["game"]["id"], new_slot["id"],
                    reason="Earlier puck drop", actor_id=ADMIN)
                self.assertNotIn("error", moved, (label, moved))
                republished = fx["api"].publish_game(
                    fx["game"]["id"], actor_id=ADMIN)
                self.assertNotIn("error", republished,
                                 (label, republished))

                before = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                fx["api"].roster.clock = lambda: earlier_drop
                result = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", result, (label, result))
                self.assertIn("expired", result["error"]["message"].lower(),
                              (label, result))
                after = self._game_write_state(
                    fx["store"], fx["game"]["id"])
                self._assert_only_expiry_write(
                    before, after, fx["player"]["id"],
                    (label, result, after), reason="accept_after_deadline")

    def test_enrollment_reuses_its_post_lock_decision_time(self):
        """A request straddling puck drop cannot timestamp itself late."""
        for label, fx in self._each():
            with self.subTest(backend=label):
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                before_drop = drop - timedelta(microseconds=1)
                ticks = iter((before_drop,))
                fx["api"].roster.clock = lambda: next(ticks, drop)
                # Remove only the two pre-lock visibility reads so the first
                # clock sample belongs to the locked transition decision. A
                # second sample for enrolled_at would now land at puck drop.
                with mock.patch.object(
                        fx["api"].roster, "_cross_team_opt_in_visible",
                        return_value=True):
                    enrolled = fx["api"].enroll_substitute(
                        fx["game"]["id"], fx["player"]["id"],
                        target_team_id=fx["team4"]["id"])
                self.assertNotIn("error", enrolled, (label, enrolled))
                self.assertEqual(
                    enrolled["enrolled_at"], before_drop.isoformat(),
                    (label, enrolled))

    def test_offer_reuses_its_post_lock_decision_time(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                before_drop = drop - timedelta(microseconds=1)
                ticks = iter((before_drop,))
                fx["api"].roster.clock = lambda: next(ticks, drop)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))
                self.assertEqual(
                    offered["offered_at"], before_drop.isoformat(),
                    (label, offered))
                self.assertEqual(
                    offered["offer_expires_at"], drop.isoformat(),
                    (label, offered))

    def test_accept_reuses_one_decision_time_for_offer_and_roster(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                fx["api"].roster.clock = lambda: drop - timedelta(minutes=10)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", offered, (label, offered))

                decision_at = drop - timedelta(microseconds=2)
                ticks = iter((decision_at,))
                fx["api"].roster.clock = lambda: next(ticks, drop)
                accepted = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertNotIn("error", accepted, (label, accepted))
                sub = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                entry = fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(sub.accepted_at, decision_at, label)
                self.assertEqual(entry.selected_at, decision_at, label)
                self.assertEqual(entry.updated_at, decision_at, label)

    def test_override_reuses_one_decision_time_for_offer_and_roster(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                drop = fx["store"].get_game(fx["game"]["id"]).start_time
                decision_at = drop - timedelta(microseconds=1)
                ticks = iter((decision_at,))
                fx["api"].roster.clock = lambda: next(ticks, drop)
                accepted = fx["api"].add_substitute_to_roster(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertNotIn("error", accepted, (label, accepted))
                sub = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                entry = fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(sub.accepted_at, decision_at, label)
                self.assertEqual(entry.selected_at, decision_at, label)
                self.assertEqual(entry.updated_at, decision_at, label)

    def test_hidden_draft_or_past_enrollment_is_cleanup_not_outreach(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                original = fx["store"].get_game(fx["game"]["id"])
                original_start = original.start_time
                original_end = original.end_time
                cases = (
                    ("unpublished", False, False, original_start, original_end),
                    ("draft", True, True, original_start, original_end),
                    ("past", True, False,
                     datetime(2025, 12, 31, 18, tzinfo=UTC),
                     datetime(2025, 12, 31, 19, tzinfo=UTC)),
                )
                for state, published, is_draft, start, end in cases:
                    with self.subTest(backend=label, state=state):
                        game = fx["store"].get_game(fx["game"]["id"])
                        game.published = published
                        game.is_draft = is_draft
                        game.start_time = start
                        game.end_time = end
                        game.locked = False
                        game.cancelled = False
                        _save_game(fx["store"], game)

                        home = fx["api"].get_player_home(
                            fx["player"]["id"])
                        (choice,) = [
                            row for row in home["substitute_opportunities"]
                            if row["game_id"] == fx["game"]["id"]
                            and row.get("target_team_id")
                            == fx["team4"]["id"]]
                        self.assertTrue(choice["needs_cleanup"],
                                        (label, state, choice))
                        self.assertTrue(choice["can_withdraw"],
                                        (label, state, choice))
                        self.assertFalse(choice["can_enroll"],
                                         (label, state, choice))

                        board = fx["api"].get_board(
                            fx["game"]["id"], fx["team4"]["id"],
                            viewer_role=Role.COACH)
                        (lineup_row,) = [
                            row for row in board["players"]
                            if row["id"] == fx["player"]["id"]]
                        self.assertEqual(lineup_row["group"], "substitute",
                                         (label, state, lineup_row))
                        self.assertEqual(lineup_row["sub_status"], "enrolled",
                                         (label, state, lineup_row))
                        self.assertFalse(lineup_row["eligible"],
                                         (label, state, lineup_row))
                        queue = fx["api"].get_substitute_candidates(
                            fx["game"]["id"], fx["team4"]["id"])
                        self.assertNotIn(
                            fx["player"]["id"],
                            [row["player_id"]
                             for row in queue["candidates"]],
                            (label, state, queue))

                        for command, invoke in (
                            ("offer", lambda: fx["api"].offer_substitute(
                                fx["game"]["id"], fx["player"]["id"],
                                authorized_team_id=fx["team4"]["id"])),
                            ("override",
                             lambda: fx["api"].add_substitute_to_roster(
                                 fx["game"]["id"], fx["player"]["id"],
                                 authorized_team_id=fx["team4"]["id"])),
                        ):
                            self._assert_refused_without_writes(
                                fx, invoke, (label, state, command))

    def test_only_target_queue_can_offer_and_accept_seats_target_side(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertNotIn("error", enrolled, enrolled)
                before = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team4"]["id"])
                before_row = next(
                    c for c in before["candidates"]
                    if c["player_id"] == fx["player"]["id"])
                self.assertFalse(before_row["can_offer"], label)

                _Fixture.open_team4_slot(fx)
                home = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team4"]["id"])
                away = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team5"]["id"])
                self.assertIn(
                    fx["player"]["id"],
                    [c["player_id"] for c in home["candidates"]], label)
                self.assertNotIn(
                    fx["player"]["id"],
                    [c["player_id"] for c in away["candidates"]], label)
                after_row = next(
                    c for c in home["candidates"]
                    if c["player_id"] == fx["player"]["id"])
                self.assertTrue(after_row["can_offer"], label)

                wrong = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team5"]["id"])
                self.assertIn("error", wrong, wrong)

                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered["status"], "offered", offered)
                self.assertNotIn("source_membership_id", offered, offered)
                self.assertNotIn("source_team_id", offered, offered)
                offer_rows = fx["api"].get_player_home(
                    fx["player"]["id"])["substitute_offers"]
                self.assertEqual(
                    [(r["game_id"], r["target_team_id"])
                     for r in offer_rows],
                    [(fx["game"]["id"], fx["team4"]["id"])], label)
                opportunity_rows = fx["api"].get_player_home(
                    fx["player"]["id"])["substitute_opportunities"]
                self.assertNotIn(
                    (fx["game"]["id"], fx["team4"]["id"]),
                    [(r["game_id"], r["target_team_id"])
                     for r in opportunity_rows], label)

                accepted = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(accepted["status"], "accepted", accepted)
                self.assertNotIn("source_membership_id", accepted, accepted)
                self.assertNotIn("source_team_id", accepted, accepted)
                entry = fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIsNotNone(entry, label)
                self.assertEqual(entry.team_side, fx["team4"]["id"], label)

    def test_wrong_side_coach_cannot_probe_activity_or_source_provenance(self):
        """Target ownership is checked before every private source fact."""
        commands = ("offer", "coach_accept", "override_add")
        ran = []
        for backend, store in self._stores():
            self._assert_backend(backend, store)
            try:
                for command in commands:
                    with self.subTest(backend=backend, command=command):
                        fx = _Fixture().build(store)
                        self._enroll_and_open(fx)
                        if command == "coach_accept":
                            offered = fx["api"].offer_substitute(
                                fx["game"]["id"], fx["player"]["id"],
                                authorized_team_id=fx["team4"]["id"])
                            self.assertEqual(offered.get("status"), "offered",
                                             offered)

                        def invoke():
                            if command == "offer":
                                return fx["api"].offer_substitute(
                                    fx["game"]["id"], fx["player"]["id"],
                                    authorized_team_id=fx["team5"]["id"])
                            if command == "coach_accept":
                                return fx["api"].accept_substitute(
                                    fx["game"]["id"], fx["player"]["id"],
                                    authorized_team_id=fx["team5"]["id"])
                            return fx["api"].add_substitute_to_roster(
                                fx["game"]["id"], fx["player"]["id"],
                                authorized_team_id=fx["team5"]["id"])

                        before_audit = list(fx["store"].audit_for_game(
                            fx["game"]["id"]))
                        baseline = invoke()
                        self.assertEqual(
                            baseline.get("error", {}).get("code"),
                            "forbidden", baseline)

                        player = fx["store"].get_player(fx["player"]["id"])
                        player.is_active = False
                        _save_player(fx["store"], player)
                        inactive = invoke()
                        self.assertEqual(inactive, baseline,
                                         (backend, command, inactive))

                        player = fx["store"].get_player(fx["player"]["id"])
                        player.is_active = True
                        _save_player(fx["store"], player)
                        end_membership_directly(
                            fx["store"], fx["source_membership_id"],
                            "released")
                        ended = invoke()
                        self.assertEqual(ended, baseline,
                                         (backend, command, ended))
                        self.assertEqual(
                            fx["store"].audit_for_game(fx["game"]["id"]),
                            before_audit, (backend, command))
                        self.assertIsNone(
                            fx["store"].roster_entry_for_player(
                                fx["game"]["id"], fx["player"]["id"]),
                            (backend, command))
                        if isinstance(store, SqlStore):
                            store.clear_all_data()
            finally:
                ran.append(backend)
                self._close(store)
        self._assert_ran(ran)

    def test_stale_cross_offer_stays_visible_but_cannot_be_accepted(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)
                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "released")
                replacement = fx["api"].create_season_roster_membership(
                    fx["player"]["id"], fx["game"]["league_season_id"],
                    fx["team1"]["id"], position="defense",
                    reason="replacement stint", actor_id=ADMIN)
                self.assertNotIn("error", replacement, replacement)

                home = fx["api"].get_player_home(fx["player"]["id"])
                (offer,) = home["substitute_offers"]
                self.assertTrue(offer["cross_team"], offer)
                self.assertEqual(offer["target_team_id"],
                                 fx["team4"]["id"], offer)
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertFalse(detail["can_accept_offer"], detail)
                self.assertTrue(detail["can_decline_offer"], detail)
                self.assertIn("original league season and division",
                              detail["blocked_reason"], detail)
                refused = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", refused, refused)
                self.assertIsNone(fx["store"].roster_entry_for_player(
                    fx["game"]["id"], fx["player"]["id"]), label)
                declined = fx["api"].decline_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(declined.get("status"), "declined",
                                 declined)

    def test_unpublished_cross_offer_remains_declineable_from_home(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)
                game = fx["store"].get_game(fx["game"]["id"])
                game.published = False
                game.is_draft = True
                _save_game(fx["store"], game)

                home = fx["api"].get_player_home(fx["player"]["id"])
                (offer,) = home["substitute_offers"]
                self.assertEqual(offer["target_team_id"],
                                 fx["team4"]["id"], offer)
                self.assertNotIn("team_status", offer, offer)
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertFalse(detail["can_accept_offer"], detail)
                self.assertTrue(detail["can_decline_offer"], detail)
                with mock.patch.object(
                        fx["api"].guardians, "verified_junior_ids",
                        return_value=[fx["player"]["id"]]):
                    guardian = fx["api"].get_guardian_home("guardian")
                (guardian_offer,) = guardian["juniors"][0][
                    "substitute_offers"]
                self.assertFalse(guardian_offer["can_accept_offer"],
                                 guardian_offer)
                self.assertTrue(guardian_offer["can_decline_offer"],
                                guardian_offer)
                self._assert_refused_without_writes(
                    fx,
                    lambda: fx["api"].accept_substitute(
                        fx["game"]["id"], fx["player"]["id"]),
                    (label, "unpublished-offer", "accept"))
                declined = fx["api"].decline_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(declined.get("status"), "declined",
                                 declined)

    def test_archived_canonical_season_disables_every_offer_action(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered.get("status"), "offered", offered)

                canonical_season_id = fx["game"]["season_id"]
                other_season_id = next(
                    season.id for season in fx["store"].all_seasons()
                    if season.id != canonical_season_id)
                game = fx["store"].get_game(fx["game"]["id"])
                game.season_id = other_season_id
                _save_game(fx["store"], game)
                fx["api"].setup.archive_season(
                    canonical_season_id, actor_id=ADMIN, reason="season over")

                home = fx["api"].get_player_home(fx["player"]["id"])
                (home_offer,) = home["substitute_offers"]
                detail = fx["api"].get_substitute_opportunity(
                    fx["player"]["id"], fx["game"]["id"],
                    target_team_id=fx["team4"]["id"])
                with mock.patch.object(
                        fx["api"].guardians, "verified_junior_ids",
                        return_value=[fx["player"]["id"]]):
                    guardian = fx["api"].get_guardian_home("guardian")
                (guardian_offer,) = guardian["juniors"][0][
                    "substitute_offers"]
                for surface, row in (
                        ("home", home_offer),
                        ("detail", detail),
                        ("guardian", guardian_offer)):
                    self.assertFalse(row["can_accept_offer"],
                                     (label, surface, row))
                    self.assertFalse(row["can_decline_offer"],
                                     (label, surface, row))
                    self.assertTrue(row["blocked_reason"],
                                    (label, surface, row))
                self.assertFalse(detail["can_accept"], detail)
                self.assertFalse(detail["can_withdraw"], detail)
                canonical_block = fx["api"].roster.game_mutation_block_reason(
                    fx["store"].get_game(fx["game"]["id"]))
                self.assertIn("archived and read-only", canonical_block,
                              (label, canonical_block))

                board = fx["api"].get_board(
                    fx["game"]["id"], fx["team4"]["id"],
                    viewer_role=Role.COACH)
                (lineup_row,) = [
                    row for row in board["players"]
                    if row["id"] == fx["player"]["id"]]
                self.assertEqual(lineup_row["sub_status"], "offered",
                                 lineup_row)
                self.assertFalse(lineup_row["eligible"], lineup_row)
                queue = fx["api"].get_substitute_candidates(
                    fx["game"]["id"], fx["team4"]["id"])
                self.assertNotIn(
                    fx["player"]["id"],
                    [row["player_id"] for row in queue["candidates"]], queue)

                for command, invoke in (
                    ("accept", lambda: fx["api"].accept_substitute(
                        fx["game"]["id"], fx["player"]["id"])),
                    ("decline", lambda: fx["api"].decline_substitute(
                        fx["game"]["id"], fx["player"]["id"])),
                    ("override", lambda: fx["api"].add_substitute_to_roster(
                        fx["game"]["id"], fx["player"]["id"],
                        authorized_team_id=fx["team4"]["id"])),
                ):
                    refused = self._assert_refused_without_writes(
                        fx, invoke, (label, "archived-offer", command))
                    self.assertIn(
                        "archived and read-only",
                        refused["error"]["message"],
                        (label, command, refused))

    def test_ended_source_membership_cannot_be_offered(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "released")
                result = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertIn("error", result, result)
                self.assertNotEqual(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]
                    ).status.value,
                    "offered", label)

    def test_replacement_membership_cannot_resurrect_stale_enrollment(self):
        """Eligibility belongs to the exact stint recorded at enrollment.

        Ending that stint and creating a fresh ACTIVE row on the same source
        team must not silently transfer an old willingness record to a new
        contract/registration period.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "released")
                replacement = fx["api"].create_season_roster_membership(
                    fx["player"]["id"], fx["game"]["league_season_id"],
                    fx["team1"]["id"], position="defense",
                    reason="new stint", actor_id=ADMIN)
                self.assertNotIn("error", replacement, replacement)

                result = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertIn("error", result, result)
                self.assertNotEqual(
                    fx["store"].substitute_for_player(
                        fx["game"]["id"], fx["player"]["id"]
                    ).status.value,
                    "offered", label)

    def test_target_team_membership_cannot_replace_recorded_source(self):
        """A new direct game-side membership cannot launder stale provenance.

        The row was admitted through Team 1's exact membership.  If that stint
        ends and the player later joins participating Team 4, offer-time
        revalidation must still check the recorded Team 1 source markers.  The
        direct-context shortcut for a current game-side player must not adopt
        the older cross-team enrollment.
        """
        for label, fx in self._each():
            with self.subTest(backend=label):
                enrolled = fx["api"].enroll_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    target_team_id=fx["team4"]["id"])
                self.assertEqual(enrolled["status"], "enrolled", enrolled)
                end_membership_directly(
                    fx["store"], fx["source_membership_id"], "released")

                replacement = fx["api"].create_season_roster_membership(
                    fx["player"]["id"], fx["game"]["league_season_id"],
                    fx["team4"]["id"], position="defense",
                    reason="joined target after volunteering", actor_id=ADMIN)
                self.assertNotIn("error", replacement, replacement)
                _Fixture.open_team4_slot(fx)

                before = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(before.status.value, "enrolled", label)
                self.assertEqual(
                    before.source_membership_id,
                    fx["source_membership_id"], label)
                self.assertIsNone(
                    fx["store"].roster_entry_for_player(
                        fx["game"]["id"], fx["player"]["id"]), label)


                result = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertIn("error", result, result)

                after = fx["store"].substitute_for_player(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertEqual(after.status.value, "enrolled", label)
                self.assertEqual(
                    after.source_membership_id,
                    fx["source_membership_id"], label)
                self.assertIsNone(
                    fx["store"].roster_entry_for_player(
                        fx["game"]["id"], fx["player"]["id"]), label)


    def test_source_registration_drift_cannot_be_offered(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                reg = fx["store"].get_season_team_registration(
                    fx["reg1"]["id"])
                reg.active = False
                _save_registration(fx["store"], reg)
                result = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertIn("error", result, result)

    def test_target_registration_drift_cannot_be_accepted(self):
        for label, fx in self._each():
            with self.subTest(backend=label):
                self._enroll_and_open(fx)
                offered = fx["api"].offer_substitute(
                    fx["game"]["id"], fx["player"]["id"],
                    authorized_team_id=fx["team4"]["id"])
                self.assertEqual(offered["status"], "offered", offered)
                reg = fx["store"].get_season_team_registration(
                    fx["reg4"]["id"])
                reg.active = False
                _save_registration(fx["store"], reg)

                result = fx["api"].accept_substitute(
                    fx["game"]["id"], fx["player"]["id"])
                self.assertIn("error", result, result)
                self.assertIsNone(
                    fx["store"].roster_entry_for_player(
                        fx["game"]["id"], fx["player"]["id"]), label)


class CrossTeamCoachActionsOverHttp(unittest.TestCase):
    """The target coach can manage a volunteer who is on neither game side."""

    PASSWORD = "Passw0rd!x287"

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.fx = _Fixture().build(srv.STATE.api.store)
        # The real HTTP handler owns a separate facade over this shared store;
        # keep it on the fixture's deterministic pre-game clock.
        srv.STATE.api.roster.clock = self.fx["api"].roster.clock
        for username, team_key in (
                ("cross_source_coach", "team1"),
                ("cross_target_coach", "team4"),
                ("cross_opponent_coach", "team5")):
            account = self.fx["api"].create_user_account(
                username, self.PASSWORD, "coach",
                scope={"team_id": self.fx[team_key]["id"]}, actor_id=ADMIN)
            self.assertNotIn("error", account, account)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)
        self.openers = {
            name: self._login(name)
            for name in ("cross_source_coach", "cross_target_coach",
                         "cross_opponent_coach")}

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _request(self, opener, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def _login(self, username):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._request(
            opener, "POST", "/api/auth/login",
            {"username": username, "password": self.PASSWORD})
        self.assertEqual(status, 200, body)
        return opener

    def _state(self):
        return _CrossTeamContract._game_write_state(
            self.fx["store"], self.fx["game"]["id"])

    def test_target_coach_can_offer_cross_team_volunteer(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        offer_path = f"/api/games/{gid}/substitutes/{pid}/offer"

        # Before the player opts in, a non-owning coach receives the same
        # generic scope refusal they receive after the private row exists.
        absent_status, absent_body = self._request(
            self.openers["cross_source_coach"], "POST", offer_path, {})
        self.assertEqual(absent_status, 403, absent_body)

        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        _Fixture.open_team4_slot(self.fx)

        for username in ("cross_source_coach", "cross_opponent_coach"):
            status, body = self._request(
                self.openers[username], "POST", offer_path, {})
            self.assertEqual(status, absent_status, (username, body))
            self.assertEqual(body["error"]["code"],
                             absent_body["error"]["code"], body)
            self.assertEqual(body["error"]["message"],
                             absent_body["error"]["message"], body)

        status, offered = self._request(
            self.openers["cross_target_coach"], "POST", offer_path, {})
        self.assertEqual(status, 200, offered)
        self.assertEqual(offered.get("status"), "offered", offered)
        self.assertEqual(offered.get("team_id"), self.fx["team4"]["id"],
                         offered)

    def test_target_coach_can_override_then_manage_the_durable_roster_row(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        _Fixture.open_team4_slot(self.fx)

        add_path = f"/api/games/{gid}/substitutes/{pid}/add-to-roster"
        before = self._state()
        for username in ("cross_source_coach", "cross_opponent_coach"):
            status, body = self._request(
                self.openers[username], "POST", add_path, {})
            self.assertEqual(status, 403, (username, body))
            self.assertEqual(body["error"]["message"],
                             "A coach can only manage their own team's players.")
            self.assertEqual(self._state(), before, username)

        status, seated = self._request(
            self.openers["cross_target_coach"], "POST",
            add_path, {})
        self.assertEqual(status, 200, seated)
        self.assertEqual(seated.get("team_side"), self.fx["team4"]["id"],
                         seated)
        self.assertEqual(seated.get("player_id"), pid, seated)

        for path, body in (
                (f"/api/games/{gid}/availability",
                 {"player_id": pid, "availability_status": "unavailable"}),
                (f"/api/games/{gid}/roster/remove", {"player_id": pid})):
            before = self._state()
            for username in ("cross_source_coach", "cross_opponent_coach"):
                status, refused = self._request(
                    self.openers[username], "POST", path, body)
                self.assertEqual(status, 403, (username, path, refused))
                self.assertEqual(self._state(), before, (username, path))
            status, result = self._request(
                self.openers["cross_target_coach"], "POST", path, body)
            self.assertEqual(status, 200, (path, result))

    def test_coaches_cannot_remove_player_owned_cross_team_opt_in(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        end_membership_directly(
            self.fx["store"], self.fx["source_membership_id"])

        path = f"/api/games/{gid}/substitutes/withdraw"
        before = self._state()
        for username in ("cross_source_coach", "cross_target_coach",
                         "cross_opponent_coach"):
            status, refused = self._request(
                self.openers[username], "POST", path, {"player_id": pid})
            self.assertEqual(status, 403, (username, refused))
            self.assertEqual(self._state(), before, username)

    def test_target_coach_cannot_answer_cross_team_offer_for_player(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        _Fixture.open_team4_slot(self.fx)
        offered = self.fx["api"].offer_substitute(
            gid, pid, authorized_team_id=self.fx["team4"]["id"])
        self.assertEqual(offered.get("status"), "offered", offered)

        before = self._state()
        for response in ("accept", "decline"):
            path = f"/api/games/{gid}/substitutes/{pid}/{response}"
            status, refused = self._request(
                self.openers["cross_target_coach"], "POST", path, {})
            self.assertEqual(status, 403, (response, refused))
            self.assertEqual(self._state(), before, response)

        # A coach seats the player through the explicit override command;
        # they never forge the player's answer to an offer.
        status, seated = self._request(
            self.openers["cross_target_coach"], "POST",
            f"/api/games/{gid}/substitutes/{pid}/add-to-roster", {})
        self.assertEqual(status, 200, seated)
        self.assertEqual(seated.get("team_side"), self.fx["team4"]["id"],
                         seated)

    def test_cross_row_does_not_widen_create_routes_or_malformed_ownership(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)

        for path, body in (
                (f"/api/games/{gid}/substitutes/enroll", {"player_id": pid}),
                (f"/api/games/{gid}/substitutes/add-candidate",
                 {"player_id": pid}),
                (f"/api/games/{gid}/roster/select", {"player_ids": [pid]})):
            status, refused = self._request(
                self.openers["cross_target_coach"], "POST", path, body)
            self.assertEqual(status, 403, (path, refused))

        sub = self.fx["store"].substitute_for_player(gid, pid)
        sub.source_team_id = None
        self.fx["store"].save_substitute(sub)
        before = self._state()
        status, refused = self._request(
            self.openers["cross_target_coach"], "POST",
            f"/api/games/{gid}/substitutes/{pid}/offer", {})
        self.assertEqual(status, 403, refused)
        self.assertEqual(self._state(), before)


class CrossTeamExpiredOfferOverHttp(unittest.TestCase):
    """An elapsed offer remains dismissible through the real player route."""

    PASSWORD = "Passw0rd!x287"

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.fx = _Fixture().build(srv.STATE.api.store)
        # STATE.reset replaces the store but deliberately preserves the API
        # object; reset its injectable clock so one deadline test cannot leak
        # a frozen timestamp into the next real-HTTP test.
        srv.STATE.api.roster.clock = self.fx["api"].roster.clock
        _Fixture.open_team4_slot(self.fx)
        enrolled = self.fx["api"].enroll_substitute(
            self.fx["game"]["id"], self.fx["player"]["id"],
            target_team_id=self.fx["team4"]["id"])
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        offered = self.fx["api"].offer_substitute(
            self.fx["game"]["id"], self.fx["player"]["id"],
            authorized_team_id=self.fx["team4"]["id"])
        self.assertEqual(offered.get("status"), "offered", offered)
        self.offered = offered
        account = self.fx["api"].create_user_account(
            "cross_team_player", self.PASSWORD, "player",
            scope={"player_id": self.fx["player"]["id"]}, actor_id=ADMIN)
        self.assertNotIn("error", account, account)
        self.player_user_id = account["id"]
        guardian = self.fx["api"].create_user_account(
            "cross_team_guardian", self.PASSWORD, "guardian",
            actor_id=ADMIN)
        self.assertNotIn("error", guardian, guardian)
        link = self.fx["api"].create_guardian_link(
            guardian["id"], self.fx["player"]["id"], actor_id=ADMIN)
        self.assertNotIn("error", link, link)
        verified = self.fx["api"].verify_guardian_link(
            link["id"], "signed_form", actor_id=ADMIN)
        self.assertNotIn("error", verified, verified)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._request(
            "POST", "/api/auth/login",
            {"username": "cross_team_player", "password": self.PASSWORD})
        self.assertEqual(status, 200, body)
        self.guardian_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._request(
            "POST", "/api/auth/login",
            {"username": "cross_team_guardian", "password": self.PASSWORD},
            opener=self.guardian_opener)
        self.assertEqual(status, 200, body)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _request(self, method, path, body=None, *, opener=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with (opener or self.opener).open(request) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_expired_offer_dismisses_and_releases_next_opt_in(self):
        deadline = datetime.fromisoformat(self.offered["offer_expires_at"])
        srv.STATE.api.roster.clock = lambda: deadline

        status, home = self._request("GET", "/api/me/player-home")
        self.assertEqual(status, 200, home)
        (offer,) = [row for row in home["substitute_offers"]
                    if row["game_id"] == self.fx["game"]["id"]]
        self.assertFalse(offer["can_accept_offer"], offer)
        self.assertTrue(offer["can_decline_offer"], offer)
        self.assertTrue(offer["offer_expired"], offer)

        before = _CrossTeamContract._game_write_state(
            self.fx["store"], self.fx["game"]["id"])
        status, stale = self._request(
            "POST",
            f"/api/me/substitute-opportunities/{self.fx['game']['id']}/"
            "decline-offer", {})
        self.assertEqual(status, 409, stale)
        self.assertEqual(stale.get("error", {}).get("code"),
                         "invalid_transition", stale)
        self.assertEqual(
            _CrossTeamContract._game_write_state(
                self.fx["store"], self.fx["game"]["id"]), before)

        status, dismissed = self._request(
            "POST",
            f"/api/me/substitute-opportunities/{self.fx['game']['id']}/"
            "decline-offer",
            {"target_team_id": self.fx["team4"]["id"]})
        self.assertEqual(status, 200, dismissed)
        self.assertEqual(dismissed.get("status"), "expired", dismissed)

        status, home = self._request("GET", "/api/me/player-home")
        self.assertEqual(status, 200, home)
        self.assertFalse(
            [row for row in home["substitute_offers"]
             if row["game_id"] == self.fx["game"]["id"]], home)

        # Move the trusted test clock back inside the still-upcoming window;
        # the terminal EXPIRED history must no longer occupy the unique active
        # row and the scoped route can start a new lifecycle.
        srv.STATE.api.roster.clock = lambda: datetime.fromisoformat(
            self.offered["offered_at"])
        status, reenrolled = self._request(
            "POST",
            f"/api/me/substitute-opportunities/{self.fx['game']['id']}/enroll",
            {"target_team_id": self.fx["team4"]["id"]})
        self.assertEqual(status, 200, reenrolled)
        self.assertEqual(reenrolled.get("status"), "enrolled", reenrolled)

    def test_borrowed_home_card_does_not_grant_target_private_routes(self):
        """Home can show the commitment without making Team 4 the player's team."""
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        target = self.fx["team4"]["id"]
        status, accepted = self._request(
            "POST", f"/api/me/substitute-opportunities/{gid}/accept-offer",
            {"target_team_id": target})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("team_side"), target, accepted)

        srv.STATE.api.roster.clock = lambda: datetime(
            2026, 2, 1, 17, tzinfo=UTC)
        status, home = self._request("GET", "/api/me/player-home")
        self.assertEqual(status, 200, home)
        self.assertEqual(home["next_game"]["game_id"], gid, home)
        self.assertTrue(home["next_game"]["cross_team"], home)
        self.assertIsNone(home["next_game"]["team_status"], home)
        self.assertEqual(
            home["next_game"]["attendance_status"], "confirmed", home)
        self.assertEqual(home["today_count"], 2, home)

        private_paths = (
            f"/api/games/{gid}/board",
            f"/api/games/{gid}/lineups",
            f"/api/games/{gid}/roster",
            f"/api/games/{gid}/roster-status",
            f"/api/games/{gid}/substitutes",
            f"/api/games/{gid}/availability-summary?team_id={target}",
        )
        for path in private_paths:
            with self.subTest(path=path):
                private_status, refused = self._request("GET", path)
                self.assertEqual(private_status, 403, (path, refused))
                self.assertEqual(
                    refused.get("error", {}).get("code"), "forbidden",
                    (path, refused))

        # The ordinary self-only availability alias remains the discoverable
        # Can't Play command from the card; it does not require a private
        # target-side roster read.
        status, unavailable = self._request(
            "POST", f"/api/games/{gid}/availability",
            {"player_id": pid, "availability_status": "unavailable"})
        self.assertEqual(status, 200, unavailable)
        status, after = self._request("GET", "/api/me/player-home")
        self.assertEqual(status, 200, after)
        self.assertEqual(
            after["next_game"]["game_id"], self.fx["own_game"]["id"], after)
        self.assertEqual(after["today_count"], 1, after)

    def test_stale_team4_actions_cannot_mutate_new_team5_offer(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        target4 = self.fx["team4"]["id"]
        target5 = self.fx["team5"]["id"]

        terminal = self.fx["api"].decline_substitute(gid, pid)
        self.assertEqual(terminal.get("status"), "declined", terminal)
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=target5)
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        offered = self.fx["api"].offer_substitute(
            gid, pid, authorized_team_id=target5)
        self.assertEqual(offered.get("status"), "offered", offered)

        before = _CrossTeamContract._game_write_state(self.fx["store"], gid)
        detail_path = f"/api/me/substitute-opportunities/{gid}"
        status, hidden = self._request("GET", detail_path)
        self.assertEqual(status, 404, hidden)
        status, detail = self._request(
            "GET", f"{detail_path}?target_team_id={target5}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail.get("target_team_id"), target5, detail)
        self.assertEqual(
            _CrossTeamContract._game_write_state(self.fx["store"], gid),
            before)

        generic_accept = f"/api/games/{gid}/substitutes/{pid}/accept"
        for body in ({}, {"target_team_id": target4}):
            status, refused = self._request("POST", generic_accept, body)
            self.assertEqual(status, 409, (body, refused))
            self.assertEqual(
                refused.get("error", {}).get("code"),
                "invalid_transition", (body, refused))
            self.assertEqual(
                _CrossTeamContract._game_write_state(
                    self.fx["store"], gid), before, body)

        for path, body, expected_status, expected_code in (
                (f"/api/games/{gid}/substitutes/withdraw",
                 {"player_id": pid}, 403, "forbidden"),
                (f"/api/games/{gid}/substitutes/{pid}/decline", {},
                 409, "invalid_transition")):
            status, refused = self._request("POST", path, body)
            self.assertEqual(status, expected_status, (path, refused))
            self.assertEqual(
                refused.get("error", {}).get("code"), expected_code,
                (path, refused))
            self.assertEqual(
                _CrossTeamContract._game_write_state(
                    self.fx["store"], gid), before, path)

        for verb in ("withdraw", "accept-offer", "decline-offer"):
            with self.subTest(verb=verb):
                status, refused = self._request(
                    "POST",
                    f"/api/me/substitute-opportunities/{gid}/{verb}",
                    {"target_team_id": target4})
                self.assertEqual(status, 409, (verb, refused))
                self.assertEqual(
                    refused.get("error", {}).get("code"),
                    "invalid_transition", (verb, refused))
                self.assertEqual(
                    _CrossTeamContract._game_write_state(
                        self.fx["store"], gid), before, verb)

        status, accepted = self._request(
            "POST", generic_accept, {"target_team_id": target5})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("team_side"), target5, accepted)

    def test_generic_player_decline_alias_accepts_the_exact_cross_team_target(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        target = self.fx["team4"]["id"]
        before = _CrossTeamContract._game_write_state(self.fx["store"], gid)

        status, declined = self._request(
            "POST", f"/api/games/{gid}/substitutes/{pid}/decline",
            {"target_team_id": target})
        self.assertEqual(status, 200, declined)
        self.assertEqual(declined.get("status"), "declined", declined)
        self.assertEqual(declined.get("target_team_id"), target, declined)
        after = _CrossTeamContract._game_write_state(self.fx["store"], gid)
        self.assertEqual(len(after["audit"]), len(before["audit"]) + 1, after)
        self.assertEqual(
            after["audit"][-1].action, AuditAction.SUBSTITUTE_DECLINED, after)

    def test_guardian_detail_and_responses_use_the_same_compound_identity(self):
        gid = self.fx["game"]["id"]
        pid = self.fx["player"]["id"]
        target4 = self.fx["team4"]["id"]
        target5 = self.fx["team5"]["id"]

        terminal = self.fx["api"].decline_substitute(gid, pid)
        self.assertEqual(terminal.get("status"), "declined", terminal)
        enrolled = self.fx["api"].enroll_substitute(
            gid, pid, target_team_id=target5)
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)
        offered = self.fx["api"].offer_substitute(
            gid, pid, authorized_team_id=target5)
        self.assertEqual(offered.get("status"), "offered", offered)

        detail_path = (
            f"/api/me/guardian/{pid}/substitute-opportunities/{gid}")
        status, hidden = self._request(
            "GET", detail_path, opener=self.guardian_opener)
        self.assertEqual(status, 404, hidden)
        status, hidden = self._request(
            "GET", f"{detail_path}?target_team_id={target4}",
            opener=self.guardian_opener)
        self.assertEqual(status, 404, hidden)
        status, detail = self._request(
            "GET", f"{detail_path}?target_team_id={target5}",
            opener=self.guardian_opener)
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail.get("target_team_id"), target5, detail)

        before = _CrossTeamContract._game_write_state(self.fx["store"], gid)
        for verb in ("accept-offer", "decline-offer"):
            with self.subTest(verb=verb):
                status, refused = self._request(
                    "POST", f"{detail_path}/{verb}",
                    {"target_team_id": target4},
                    opener=self.guardian_opener)
                self.assertEqual(status, 409, (verb, refused))
                self.assertEqual(
                    _CrossTeamContract._game_write_state(
                        self.fx["store"], gid), before, verb)

        status, declined = self._request(
            "POST", f"{detail_path}/decline-offer",
            {"target_team_id": target5}, opener=self.guardian_opener)
        self.assertEqual(status, 200, declined)
        self.assertEqual(declined.get("status"), "declined", declined)

    def test_same_team_detail_keeps_omitted_target_contract(self):
        gid = self.fx["own_game"]["id"]
        pid = self.fx["player"]["id"]
        enrolled = self.fx["api"].enroll_substitute(gid, pid)
        self.assertEqual(enrolled.get("status"), "enrolled", enrolled)

        status, detail = self._request(
            "GET", f"/api/me/substitute-opportunities/{gid}")
        self.assertEqual(status, 200, detail)
        self.assertFalse(detail.get("cross_team"), detail)
        self.assertEqual(detail.get("target_team_id"),
                         self.fx["team1"]["id"], detail)

        offered = self.fx["api"].offer_substitute(
            gid, pid, authorized_team_id=self.fx["team1"]["id"])
        self.assertEqual(offered.get("status"), "offered", offered)
        status, accepted = self._request(
            "POST", f"/api/games/{gid}/substitutes/{pid}/accept",
            {"actor_id": "legacy-client-supplied-actor"})
        self.assertEqual(status, 200, accepted)
        self.assertEqual(accepted.get("team_side"),
                         self.fx["team1"]["id"], accepted)
        accepted_audits = [
            row for row in self.fx["store"].audit_for_game(gid)
            if row.action == AuditAction.SUBSTITUTE_ACCEPTED]
        self.assertEqual(len(accepted_audits), 1, accepted_audits)
        self.assertEqual(accepted_audits[0].actor_id, self.player_user_id)
        self.assertNotEqual(accepted_audits[0].actor_id,
                            "legacy-client-supplied-actor")


if __name__ == "__main__":
    unittest.main()
