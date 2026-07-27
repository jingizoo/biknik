"""Persistent League context — the third axis of the active-context backend (#345).

#345 promotes League into the persistent Program/Season context bar. This file is
the BACKEND foundation: the service/store/scope layer.

It originally asserted the axis was deliberately NOT wired to transport. #360 is
the slice that wires it through authenticated HTTP, so the one assertion that
encoded "unwired" (the rendered-payload key set) now states the wired contract
instead — see ``LeagueContextBackwardCompatibilityTest``. The transport-level
proof lives in ``test_context_league_http.py``; everything here stays
service-level, and the context bar / browser assets remain untouched by both
slices.

The League axis inherits every guarantee the Program/Season axes already carry —
view-preference-not-authority, non-oracle rejection, ignore-don't-rewrite on an
invalid saved value, one serializable snapshot with bounded retry, detached
objects — and adds two of its own: a League must belong to the selected Program,
and a Season+League pair must name an EXISTING ``LeagueSeason`` binding, which
the context resolves read-only and NEVER creates.

Coverage on Memory/SQLite/PostgreSQL: migration 049 (pre-existing rows read back
``league_id = None``); authorized League enumeration for every role scope; exact
Program/Season/League selection and persistence; cross-Program and wrong-Season
binding rejection; deleted / unbound / authorization-revoked League fallback;
Program-or-Season with no available League; the binding-removal-vs-context-set
race; concurrent last-write-wins; and a backward-compatibility contract proving
the pre-#345 two-axis callers behave byte-identically.
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import (
    ActiveContext, GameType, GuardianLink, OfficialRole, Role, SeasonStatus)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import LeagueSeason, OfficialAssignment
from hockey_scheduler.services import context_scope
from hockey_scheduler.services.context_service import ContextService
from hockey_scheduler.services.league_scope import (
    exact_league_season_or_conflict)
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.sql_store import migrate

_LEAGUE_VERSION = "049_active_context_league"
ADMIN = (Role.LEAGUE_ADMIN, {})
_TS = datetime(2027, 5, 1, 9, 0, 0, tzinfo=timezone.utc)


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _program_season_league(api, pname="P1", sname="S1", lname="Gold"):
    """A Program + Season + a League BOUND to that Season. Returns ids."""
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    lid = api.create_league(sid, lname)["id"]
    return pid, sid, lid


def _registered_team(api, sid, lid, tname="Alpha"):
    """A Club+Team permanently in League ``lid`` and registered in Season ``sid``."""
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    team = api.create_team(club_id=club, name=tname, league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, team, did)
    return team


def _assign_game(api, official_id, *, season_id, team_id, league_id=None,
                 league_season_id=None, game_type=GameType.REGULAR.value):
    """Assign ``official_id`` to a Game with an EXPLICIT competition identity.

    Every field an Official's League derivation reads is passed in deliberately
    — `league_season_id` is the frozen source of truth, `season_id`/`league_id`
    are the denormalized columns it must agree with, and `game_type` decides
    whether the Game owns a League at all — so a test can construct the exact
    valid, missing, or drifted shape it means to exercise."""
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=season_id,
                            league_id=league_id,
                            league_season_id=league_season_id,
                            game_type=game_type))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=official_id,
            role=OfficialRole.REFEREE))
    return gid


def _assigned_official(api, sid, team_id, league_id):
    """An Official assigned to a well-formed REGULAR Game in ``league_id``/``sid``,
    carrying that pair's real frozen LeagueSeason."""
    oid = api.create_official("Ref")["id"]
    ls = api.store.league_season_for(league_id, sid)
    gid = _assign_game(api, oid, season_id=sid, team_id=team_id,
                       league_id=league_id, league_season_id=ls.id)
    return oid, gid


def _unbind(store, league_id, season_id):
    """Remove the LeagueSeason binding directly (the store-level effect of an
    authorized unbind), leaving the permanent League itself intact."""
    ls = store.league_season_for(league_id, season_id)
    if ls is not None:
        with store.transaction():
            store.delete_league_season(ls.id)


class LeagueContextSelectionTest(unittest.TestCase):
    """Exact Program/Season/League selection, persistence, and the two
    League-specific rules (same Program; existing binding when a Season is set)."""

    def test_exact_selection_persists_and_resolves(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)

                program, season, league = svc.set_with_league(
                    "u1", *ADMIN, pid, sid, lid)
                self.assertEqual(
                    (program.id, season.id, league.id), (pid, sid, lid), label)

                # Persisted on the row, and resolved back identically.
                saved = store.get_active_context("u1")
                self.assertEqual(saved.league_id, lid, label)
                rp, rs, rl = svc.resolve_with_league("u1", *ADMIN)
                self.assertEqual((rp.id, rs.id, rl.id), (pid, sid, lid), label)
                _close(store)

    def test_program_only_and_season_without_league_stay_first_class(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)

                # Program-only: no Season, no League.
                p, s, lg = svc.set_with_league("u1", *ADMIN, pid, None, None)
                self.assertEqual((p.id, s, lg), (pid, None, None), label)
                self.assertIsNone(store.get_active_context("u1").league_id, label)

                # Season without League: an explicit, supported selection.
                p, s, lg = svc.set_with_league("u1", *ADMIN, pid, sid, None)
                self.assertEqual((p.id, s.id, lg), (pid, sid, None), label)
                self.assertIsNone(store.get_active_context("u1").league_id, label)
                self.assertEqual(
                    svc.resolve_with_league("u1", *ADMIN)[2], None, label)
                _close(store)

    def test_league_without_season_is_allowed_when_it_matches_the_program(self):
        """A Program+League selection needs no binding — the binding rule only
        applies once a Season is also selected."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)
                p, s, lg = svc.set_with_league("u1", *ADMIN, pid, None, lid)
                self.assertEqual((p.id, s, lg.id), (pid, None, lid), label)
                self.assertEqual(
                    svc.resolve_with_league("u1", *ADMIN)[2].id, lid, label)
                _close(store)

    def test_archived_season_keeps_its_league_as_read_only_history(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, lid)

                season = store.get_season(sid)
                season.status = SeasonStatus.ARCHIVED
                store.save_season(season)

                rp, rs, rl = svc.resolve_with_league("u1", *ADMIN)
                self.assertEqual((rp.id, rs.id, rl.id), (pid, sid, lid), label)
                self.assertEqual(rs.status, SeasonStatus.ARCHIVED, label)
                _close(store)


class LeagueContextRejectionTest(unittest.TestCase):
    """Cross-Program, unbound, unauthorized and nonexistent League selections are
    all refused with ONE indistinguishable non-oracle reason."""

    def _reason(self, fn):
        try:
            fn()
        except Exception as exc:  # NotFoundError carries .details
            return getattr(exc, "details", {}).get("reason"), str(exc)
        return None, "no error raised"

    def test_cross_program_league_is_rejected(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pA, sA, lA = _program_season_league(api, "PA", "SA", "GoldA")
                pB, sB, lB = _program_season_league(api, "PB", "SB", "GoldB")
                svc = ContextService(store)
                reason, msg = self._reason(
                    lambda: svc.set_with_league("u1", *ADMIN, pA, sA, lB))
                self.assertEqual(reason, "league_not_accessible", (label, msg))
                # Nothing was persisted by the refused call.
                self.assertIsNone(store.get_active_context("u1"), label)
                _close(store)

    def test_wrong_season_binding_is_rejected_and_creates_nothing(self):
        """A League bound to S1 but NOT to S2 cannot be selected with S2 — and
        the refusal must not implicitly create the missing binding."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, lid = _program_season_league(api, "P", "S1", "Gold")
                s2 = api.create_season(pid, "S2")["id"]
                svc = ContextService(store)

                before = len(store.league_seasons_for_league(lid))
                reason, msg = self._reason(
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, lid))
                self.assertEqual(reason, "league_not_accessible", (label, msg))
                self.assertEqual(len(store.league_seasons_for_league(lid)),
                                 before, label)   # no implicit binding created
                self.assertIsNone(store.league_season_for(lid, s2), label)
                _close(store)

    def test_nonexistent_and_unauthorized_are_indistinguishable(self):
        """The rejection reason must not become an existence oracle: a League
        that does not exist and one that exists but is out of scope look the
        same to the caller."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                other = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, lid)
                coach = (Role.COACH, {"team_id": team})
                svc = ContextService(store)

                ghost_reason, _ = self._reason(
                    lambda: svc.set_with_league(
                        "u1", *coach, pid, sid, "league_does_not_exist"))
                real_reason, _ = self._reason(
                    lambda: svc.set_with_league("u1", *coach, pid, sid, other))
                self.assertEqual(ghost_reason, "league_not_accessible", label)
                self.assertEqual(real_reason, ghost_reason, label)
                _close(store)

    def test_ambiguous_binding_fails_closed(self):
        """Two LeagueSeason rows at one exact (league, season) key is corrupted
        state; it must refuse rather than pick whichever sorts first. SQL's
        ux_league_season blocks the duplicate INSERT, so this shape is only
        reachable on InMemoryStore — which is exactly why the primitive exists."""
        store = InMemoryStore()
        api = ApiService(store)
        pid, sid, lid = _program_season_league(api)
        svc = ContextService(store)
        self.assertIsNotNone(svc.set_with_league("u1", *ADMIN, pid, sid, lid)[2])

        with store.transaction():
            store.save_league_season(LeagueSeason(
                id="ls_duplicate", league_id=lid, season_id=sid))
        binding, conflicts = exact_league_season_or_conflict(store, lid, sid)
        self.assertIsNone(binding)
        self.assertEqual(len(conflicts), 2, conflicts)

        reason, msg = self._reason(
            lambda: svc.set_with_league("u2", *ADMIN, pid, sid, lid))
        self.assertEqual(reason, "league_not_accessible", msg)
        # And an already-saved selection stops resolving while ambiguous.
        self.assertIsNone(svc.resolve_with_league("u1", *ADMIN)[2])


class LeagueContextFallbackTest(unittest.TestCase):
    """An invalid saved League is IGNORED on resolve — never dangled, never
    rewritten, and never allowed to disturb the resolved Program/Season."""

    def _saved_row_intact(self, store, user_id, lid, label):
        self.assertEqual(store.get_active_context(user_id).league_id, lid,
                         f"{label}: the saved row must NOT be rewritten")

    def test_deleted_league_falls_back_to_none_without_rewriting(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, lid)

                _unbind(store, lid, sid)
                with store.transaction():
                    store.delete_league(lid)

                rp, rs, rl = svc.resolve_with_league("u1", *ADMIN)
                self.assertEqual((rp.id, rs.id), (pid, sid), label)  # unchanged
                self.assertIsNone(rl, label)
                self._saved_row_intact(store, "u1", lid, label)
                _close(store)

    def test_unbound_league_falls_back_and_restores_when_rebound(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, lid)

                _unbind(store, lid, sid)
                self.assertIsNone(svc.resolve_with_league("u1", *ADMIN)[2], label)
                self._saved_row_intact(store, "u1", lid, label)

                # Re-binding restores the saved choice — the whole point of
                # ignore-don't-rewrite.
                api.setup.create_league_season(lid, sid)
                self.assertEqual(
                    svc.resolve_with_league("u1", *ADMIN)[2].id, lid, label)
                _close(store)

    def test_revoked_league_authorization_falls_back_and_restores(self):
        """A scoped Coach whose Team transfers out of the saved League loses it
        on resolve, and gets it back when the Team returns.

        Deliberately a PROGRAM-ONLY context (no Season): with a Season selected,
        the same transfer ALSO revokes Season authorization, so the League would
        drop out through the season-mismatch path and this would pass without
        League authorization ever being consulted — green for the wrong reason.
        Isolating the Season out makes League authorization the only variable,
        which is what makes this leg load-bearing: bypassing the League
        authorization filter must make exactly this assertion fail."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)
                coach = (Role.COACH, {"team_id": team})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, pid, None, gold)
                self.assertEqual(
                    svc.resolve_with_league("u1", *coach)[2].id, gold, label)

                t = store.get_team(team)
                t.league_id = silver
                store.save_team(t)
                # The Program is still authorized (silver is under it), so the
                # Program/Season half of the context is untouched — only the
                # League is revoked.
                rp, _rs, rl = svc.resolve_with_league("u1", *coach)
                self.assertEqual(rp.id, pid, label)
                self.assertIsNone(rl, label)
                self._saved_row_intact(store, "u1", gold, label)

                t = store.get_team(team)
                t.league_id = gold
                store.save_team(t)
                self.assertEqual(
                    svc.resolve_with_league("u1", *coach)[2].id, gold, label)
                _close(store)

    def test_saved_league_is_dropped_when_program_season_falls_back(self):
        """If the saved Season is gone, the whole context falls back — the saved
        League must not be reattached to a context it was never chosen for."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                other_season = api.create_season(pid, "S2")["id"]
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, lid)

                _unbind(store, lid, sid)
                with store.transaction():
                    store.delete_season(sid)

                rp, rs, rl = svc.resolve_with_league("u1", *ADMIN)
                self.assertEqual(rp.id, pid, label)
                self.assertEqual(rs.id, other_season, label)   # fell back
                self.assertIsNone(rl, label)                   # not reattached
                _close(store)

    def test_program_or_season_with_no_league_resolves_cleanly(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]
                sid = api.create_season(pid, "S")["id"]     # no League at all
                svc = ContextService(store)

                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *ADMIN, pid, "u1"), set(), label)
                p, s, lg = svc.set_with_league("u1", *ADMIN, pid, sid, None)
                self.assertEqual((p.id, s.id, lg), (pid, sid, None), label)
                self.assertIsNone(svc.resolve_with_league("u1", *ADMIN)[2], label)
                _close(store)


class LeagueScopeMatrixTest(unittest.TestCase):
    """Authorized League ENUMERATION for every role scope — a scoped account may
    not even see a League outside its scope, let alone select one."""

    def test_global_roles_see_every_league_in_the_program(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api)
                silver = api.create_league(sid, "Silver")["id"]
                for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
                    self.assertEqual(
                        context_scope.authorized_league_ids(
                            store, role, {}, pid, "u1"),
                        {gold, silver}, (label, role))
                _close(store)

    def test_scoped_roles_see_only_their_own_league(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api)
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)

                # Coach: their own Team's permanent League only, from the
                # stored team_id. Player: resolved LIVE from player_id (never a
                # stored team_id, which could outlive a transfer) — both go
                # through the canonical `own_team_id`, so the League projection
                # cannot drift from the Team projection.
                player = api.create_player(
                    team_id=team, name="Kid", position="forward")["id"]
                for role, scope in ((Role.COACH, {"team_id": team}),
                                    (Role.PLAYER, {"player_id": player})):
                    got = context_scope.authorized_league_ids(
                        store, role, scope, pid, "u1")
                    self.assertEqual(got, {gold}, (label, role))
                    self.assertNotIn(silver, got, (label, role))

                # Official: the Leagues of the games they are assigned to.
                oid, _gid = _assigned_official(api, sid, team, gold)
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, Role.OFFICIAL, {"official_id": oid}, pid, "u1"),
                    {gold}, label)

                # Guardian: only through a VERIFIED link.
                with store.transaction():
                    store.add_guardian_link(GuardianLink(
                        id=store.next_id("glink"), guardian_user_id="g1",
                        player_id=player, created_at=_TS, verified=False))
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, Role.GUARDIAN, {}, pid, "g1"), set(), label)
                link = next(iter(store.guardian_links_for("g1")))
                link.verified = True
                store.save_guardian_link(link)
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, Role.GUARDIAN, {}, pid, "g1"), {gold}, label)
                _close(store)

    def test_unknown_or_unbound_role_fails_closed(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api)
                # A Coach with no team resolves to nothing.
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, Role.COACH, {}, pid, "u1"), set(), label)
                # An unrecognized role is empty, not "everything".
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, "not_a_role", {}, pid, "u1"), set(), label)
                # A null Program yields nothing rather than raising.
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *ADMIN, None, "u1"), set(), label)
                _close(store)

    def test_season_narrowing_requires_an_existing_binding(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, gold = _program_season_league(api, "P", "S1", "Gold")
                s2 = api.create_season(pid, "S2")["id"]
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *ADMIN, pid, "u1", season_id=s1), {gold}, label)
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *ADMIN, pid, "u1", season_id=s2), set(), label)
                _close(store)

    def test_options_offer_only_selectable_leagues(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api)
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)
                svc = ContextService(store)

                programs, _p, _s, _lg = svc.options_with_league("u1", *ADMIN)
                self.assertEqual(
                    {lg.id for (_prog, _seasons, lgs) in programs for lg in lgs},
                    {gold, silver}, label)

                programs, _p, _s, _lg = svc.options_with_league(
                    "u1", Role.COACH, {"team_id": team})
                self.assertEqual(
                    {lg.id for (_prog, _seasons, lgs) in programs for lg in lgs},
                    {gold}, label)
                _close(store)


class OfficialFrozenLeagueIdentityTest(unittest.TestCase):
    """An Official's League comes from each assigned Game's FROZEN
    ``league_season_id``, never from either Team's mutable permanent League.

    Reading the Team was a real defect caught in review: a transfer changes
    ``Team.league_id`` without rewriting historical Games, so a Team-derived
    projection simultaneously GRANTS a League the assignment never covered and
    REVOKES the one it does. Every leg here is falsifiable against that
    implementation.
    """

    def _reason(self, fn):
        try:
            fn()
        except Exception as exc:
            return getattr(exc, "details", {}).get("reason"), str(exc)
        return None, "no error raised"

    def test_team_transfer_does_not_move_the_officials_league(self):
        """Scenario 1: the assignment's League is invariant under a transfer of
        the Game's Team to another same-Program, same-Season League."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S1", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)
                oid, _gid = _assigned_official(api, sid, team, gold)
                official = (Role.OFFICIAL, {"official_id": oid})
                svc = ContextService(store)

                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *official, pid, "u1"), {gold}, label)

                # The Team leaves Gold for Silver; the Game stays in Gold/S1.
                t = store.get_team(team)
                t.league_id = silver
                store.save_team(t)

                got = context_scope.authorized_league_ids(
                    store, *official, pid, "u1")
                self.assertEqual(got, {gold}, f"{label}: assignment League moved")
                self.assertNotIn(silver, got, label)

                # The switcher must not offer Silver either.
                programs, _p, _s, _lg = svc.options_with_league("u1", *official)
                self.assertEqual(
                    {lg.id for (_pr, _se, lgs) in programs for lg in lgs},
                    {gold}, label)

                # Selecting Silver is refused with the same non-oracle reason,
                # and leaves no context row behind; Gold stays selectable.
                reason, msg = self._reason(
                    lambda: svc.set_with_league("u1", *official, pid, sid, silver))
                self.assertEqual(reason, "league_not_accessible", (label, msg))
                self.assertIsNone(store.get_active_context("u1"), label)
                self.assertEqual(
                    svc.set_with_league("u1", *official, pid, sid, gold)[2].id,
                    gold, label)
                _close(store)

    def test_league_set_is_the_union_of_assigned_game_identities(self):
        """Scenario 2: two assignments in two valid Leagues yield both — proving
        the set is keyed on Game identity, not on one Team or insertion order."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S1", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                gold_team = _registered_team(api, sid, gold, "GoldTeam")
                silver_team = _registered_team(api, sid, silver, "SilverTeam")
                oid, _gid = _assigned_official(api, sid, gold_team, gold)
                _assign_game(
                    api, oid, season_id=sid, team_id=silver_team,
                    league_id=silver,
                    league_season_id=store.league_season_for(silver, sid).id)
                official = (Role.OFFICIAL, {"official_id": oid})

                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *official, pid, "u1"), {gold, silver}, label)

                # Moving ONE Team does not disturb either assignment's League.
                t = store.get_team(gold_team)
                t.league_id = silver
                store.save_team(t)
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *official, pid, "u1"), {gold, silver}, label)
                _close(store)

    def test_exhibition_assignment_contributes_no_league(self):
        """Scenario 3: an exhibition owns no LeagueSeason, so it grants nothing
        — even though its Teams do sit in a League."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S1", "Gold")
                team = _registered_team(api, sid, gold)
                oid = api.create_official("Ref")["id"]
                _assign_game(api, oid, season_id=sid, team_id=team,
                             league_id=None, league_season_id=None,
                             game_type=GameType.EXHIBITION.value)
                official = (Role.OFFICIAL, {"official_id": oid})
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, *official, pid, "u1"), set(), label)
                _close(store)

    def test_missing_or_drifted_identity_fails_closed(self):
        """Scenario 4: a regular Game whose frozen identity is absent, dangling,
        cross-Season, or League-mismatched exposes NO League."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, gold = _program_season_league(api, "P", "S1", "Gold")
                s2 = api.create_season(pid, "S2")["id"]
                silver = api.create_league(s1, "Silver")["id"]
                team = _registered_team(api, s1, gold)
                gold_ls = store.league_season_for(gold, s1).id
                other_ls = api.setup.create_league_season(gold, s2).id

                cases = {
                    # A regular Game with no frozen identity at all.
                    "missing": dict(league_id=gold, league_season_id=None),
                    # Points at a LeagueSeason that no longer exists.
                    "dangling": dict(league_id=gold,
                                     league_season_id="ls_does_not_exist"),
                    # Binding's Season disagrees with the Game's own season_id.
                    "cross_season": dict(league_id=gold,
                                         league_season_id=other_ls),
                    # Binding's League disagrees with the Game's own league_id.
                    "league_mismatch": dict(league_id=silver,
                                            league_season_id=gold_ls),
                }
                for name, kwargs in cases.items():
                    oid = api.create_official(f"Ref-{name}")["id"]
                    _assign_game(api, oid, season_id=s1, team_id=team, **kwargs)
                    self.assertEqual(
                        context_scope.authorized_league_ids(
                            store, Role.OFFICIAL, {"official_id": oid},
                            pid, "u1"),
                        set(), f"{label}/{name} did not fail closed")

                # A well-formed assignment still resolves, so the guard above is
                # rejecting drift specifically — not refusing everything.
                ok_oid, _gid = _assigned_official(api, s1, team, gold)
                self.assertEqual(
                    context_scope.authorized_league_ids(
                        store, Role.OFFICIAL, {"official_id": ok_oid},
                        pid, "u1"),
                    {gold}, label)
                _close(store)


class LeagueContextRaceTest(unittest.TestCase):
    """Concurrency: the binding-removal-vs-selection race and last-write-wins."""

    def test_unbind_versus_set_never_persists_an_unbound_pair(self):
        """Whatever the interleaving, the outcome is one of exactly two
        consistent states — the selection was refused, or it committed and the
        saved pair still names a real binding at resolve time. It must never
        leave a persisted Season+League pair with no binding behind it."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)
                outcome = {}
                barrier = threading.Barrier(2)

                def setter():
                    barrier.wait()
                    try:
                        svc.set_with_league("u1", *ADMIN, pid, sid, lid)
                        outcome["set"] = "committed"
                    except Exception as exc:
                        outcome["set"] = getattr(
                            exc, "details", {}).get("reason", str(exc))

                def unbinder():
                    barrier.wait()
                    _unbind(store, lid, sid)

                threads = [threading.Thread(target=setter),
                           threading.Thread(target=unbinder)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertIn(outcome["set"],
                              ("committed", "league_not_accessible"),
                              (label, outcome))
                # Resolve is the invariant that matters: a League only ever
                # resolves while its binding genuinely exists.
                _p, _s, resolved = svc.resolve_with_league("u1", *ADMIN)
                if resolved is not None:
                    self.assertIsNotNone(
                        store.league_season_for(resolved.id, sid),
                        f"{label}: resolved a League with no binding")
                _close(store)

    def test_concurrent_league_writes_leave_one_row_last_wins(self):
        for label, store in _backends():
            if label == "postgres":
                continue   # covered by the store-level upsert parity suite
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api)
                silver = api.create_league(sid, "Silver")["id"]
                svc = ContextService(store)
                barrier = threading.Barrier(2)
                errs = []

                def w(league_id):
                    try:
                        barrier.wait()
                        svc.set_with_league("u1", *ADMIN, pid, sid, league_id)
                    except Exception as exc:
                        errs.append(f"{league_id}:{exc}")

                threads = [threading.Thread(target=w, args=(lg,))
                           for lg in (gold, silver)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertEqual(errs, [], label)
                saved = store.get_active_context("u1")
                self.assertIn(saved.league_id, (gold, silver), label)
                _close(store)


class LeagueContextBackwardCompatibilityTest(unittest.TestCase):
    """The pre-#345 two-axis contract is byte-identical. `api/service.py` unpacks
    `resolve`/`set` as 2-tuples and `options` as a 3-tuple; a change in any of
    those shapes would break the transport layer this slice deliberately does
    not touch."""

    def test_resolve_and_set_still_return_two_tuples(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                svc = ContextService(store)

                result = svc.set("u1", *ADMIN, pid, sid)
                self.assertEqual(len(result), 2, label)
                self.assertEqual((result[0].id, result[1].id), (pid, sid), label)

                resolved = svc.resolve("u1", *ADMIN)
                self.assertEqual(len(resolved), 2, label)
                self.assertEqual(
                    (resolved[0].id, resolved[1].id), (pid, sid), label)

                programs, sel_p, sel_s = svc.options("u1", *ADMIN)
                self.assertEqual(sel_p.id, pid, label)
                for entry in programs:
                    self.assertEqual(len(entry), 2, label)   # (program, seasons)
                _close(store)

    def test_two_axis_set_clears_a_previously_saved_league(self):
        """A League must never survive onto a Program/Season it wasn't chosen
        for — the legacy entry point clears it rather than carrying it over."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                s2 = api.create_season(pid, "S2")["id"]
                svc = ContextService(store)

                svc.set_with_league("u1", *ADMIN, pid, sid, lid)
                self.assertEqual(store.get_active_context("u1").league_id, lid,
                                 label)
                svc.set("u1", *ADMIN, pid, s2)
                self.assertIsNone(store.get_active_context("u1").league_id, label)
                self.assertIsNone(svc.resolve_with_league("u1", *ADMIN)[2], label)
                _close(store)

    def test_context_payload_carries_the_league_additively(self):
        """#360 supersedes this slice's "unwired" assertion ON PURPOSE.

        PR #356 (this file's original slice) asserted the rendered payload was
        UNCHANGED by the new column, because the League axis was deliberately
        not wired to transport yet. #360 is the slice that wires it, so the
        contract is now: the two pre-existing axes keep their exact keys and
        values, and `league_id`/`league` are ADDED. Both new keys are always
        present — only their value varies — so a client never has to
        distinguish "absent" from "null"."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                ContextService(store).set_with_league(
                    "u1", *ADMIN, pid, sid, lid)
                view = api.get_active_context("u1", *ADMIN)
                self.assertEqual(
                    set(view),
                    {"program_id", "season_id", "league_id", "read_only",
                     "program", "season", "league"},
                    label)
                # The pre-existing axes are untouched, and the League is the one
                # that was actually saved — rendered as a full object, not just
                # an id, exactly like Program/Season.
                self.assertEqual((view["program_id"], view["season_id"]),
                                 (pid, sid), label)
                self.assertEqual(view["league_id"], lid, label)
                self.assertEqual(view["league"]["id"], lid, label)
                _close(store)

    def test_context_payload_keeps_league_keys_present_when_none_is_selected(self):
        """A null League is a first-class state, not an error or an absence: the
        keys stay present and null for a Season-without-League selection."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, lid = _program_season_league(api)
                ContextService(store).set("u1", *ADMIN, pid, sid)
                view = api.get_active_context("u1", *ADMIN)
                self.assertIn("league_id", view, label)
                self.assertIn("league", view, label)
                self.assertIsNone(view["league_id"], label)
                self.assertIsNone(view["league"], label)
                self.assertEqual((view["program_id"], view["season_id"]),
                                 (pid, sid), label)
                _close(store)

    def test_legacy_positional_construction_still_means_the_same_thing(self):
        """`ActiveContext("u", "p", "s", ts)` predates this change and is used
        positionally in the existing suite. `league_id` is appended LAST
        precisely so this cannot silently reinterpret `ts` as a League id."""
        ctx = ActiveContext("u", "p", "s", _TS)
        self.assertEqual(
            (ctx.id, ctx.program_id, ctx.season_id, ctx.updated_at, ctx.league_id),
            ("u", "p", "s", _TS, None))


class LeagueContextMigrationTest(unittest.TestCase):
    """Migration 049 on a real file-backed SQLite / PostgreSQL database: an
    ADOPTED pre-049 row reads back with `league_id = None`, and a League
    selection survives a genuine close/reopen."""

    def _locations(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._tmp = path
        yield "sqlite", path
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", url

    def tearDown(self):
        if getattr(self, "_tmp", None) and os.path.exists(self._tmp):
            os.remove(self._tmp)

    def test_pre_049_row_adopts_as_null_league_and_new_value_persists(self):
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                # Simulate a pre-049 database that already holds a selection:
                # drop the column and de-register the migration, leaving 044's
                # own table and its row untouched.
                with store.transaction():
                    cur = store.conn.cursor()
                    cur.execute(store.dialect.sql(
                        "INSERT INTO user_active_context "
                        "(id, program_id, season_id, updated_at) "
                        "VALUES (?, ?, ?, ?)"),
                        ("legacy_user", "prog_1", "seas_1", _TS.isoformat()))
                    cur.execute(
                        "ALTER TABLE user_active_context DROP COLUMN league_id")
                    cur.execute(store.dialect.sql(
                        "DELETE FROM schema_migrations WHERE version = ?"),
                        (_LEAGUE_VERSION,))
                self.assertNotIn(_LEAGUE_VERSION,
                                 store.migration_status()["applied"], label)

                migrate(store.conn, store.dialect)
                self.assertIn(_LEAGUE_VERSION,
                              store.migration_status()["applied"], label)

                # The pre-existing row survived and adopts as league_id = None.
                legacy = store.get_active_context("legacy_user")
                self.assertEqual(
                    (legacy.program_id, legacy.season_id), ("prog_1", "seas_1"),
                    label)
                self.assertIsNone(legacy.league_id, label)

                # A new League selection round-trips durably across a reopen.
                store.set_active_context(ActiveContext(
                    id="u_new", program_id="prog_1", season_id="seas_1",
                    updated_at=_TS, league_id="lg_1"))
                store.close()
                store = SqlStore(loc)
                self.assertEqual(
                    store.get_active_context("u_new").league_id, "lg_1", label)
                self.assertIsNone(
                    store.get_active_context("legacy_user").league_id, label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


if __name__ == "__main__":
    unittest.main()
