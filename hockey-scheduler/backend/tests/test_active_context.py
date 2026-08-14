"""Per-user active Program/Season context — the selection backend (#159).

A VIEW selection only: on every resolve/set it is filtered through the caller's
REAL role + account scope (the same #211/#266/#202 rules the rest of the app
enforces), so a scoped account can neither select nor enumerate a Program/Season
outside its scope. It never grants authority. Supports Program-only selection;
honors an archived Season as a read-only historical context; and a deleted or
no-longer-authorized saved selection is ignored (fallback), the row not rewritten.

Coverage: resolve/set behavior; the full authorization MATRIX (League Admin,
Arena Manager, Viewer, Coach, Player, Official, Guardian, unknown role) on
Memory/SQLite/PostgreSQL; a subject-resolution contract proving the context
selector and the web scope guards resolve the SAME caller identity; concurrent
last-write-wins; snapshot consistency (validation and rendering read one
authoritative snapshot, so a concurrent archive/reopen/delete can never make the
payload self-contradict); migration-044 durability; and the strict authenticated
HTTP contract (including a genuinely scoped identity).
"""

import json
import os
import tempfile
from dataclasses import asdict
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import (
    ActiveContext, GuardianLink, OfficialRole, Role, SeasonStatus)
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.services import context_scope
from hockey_scheduler.services.subject_scope import own_team_id
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.store.sql_store import migrate
from hockey_scheduler.web.server import STATE, Handler

_VERSION = "044_active_context"
_LEAGUE_VERSION = "049_active_context_league"   # #345 — ALTERs 044's table
_GENERATION_VERSION = "051_active_context_generation"  # #159 review findings
                                                        # 2+5 — ALTERs 044's
                                                        # table too
ADMIN = (Role.LEAGUE_ADMIN, {})       # (role, scope) for a global operator


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


# -- scenario builders ---------------------------------------------------
def _program_season(api, pname="P1", sname="S1"):
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    return pid, sid


def _team_registered(api, sid, tname="Alpha"):
    """A League+Division+Club+Team registered in Season ``sid``. Returns team_id."""
    lid = api.create_league(sid, "Gold")["id"]
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    team = api.create_team(club_id=club, name=tname, league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, team, did)
    return team


def _official_assigned(api, sid, team_id):
    """An Official assigned to a Game in Season ``sid``. Returns (official_id, game_id)."""
    oid = api.create_official("Ref")["id"]
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=sid))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=oid,
            role=OfficialRole.REFEREE))
    return oid, gid


def _archive(store, season_id):
    s = store.get_season(season_id)
    s.status = SeasonStatus.ARCHIVED
    store.save_season(s)


def _reopen(store, season_id):
    s = store.get_season(season_id)
    s.status = SeasonStatus.ACTIVE
    s.archived_at = None
    store.save_season(s)


def _delete_program_cascade(store, program_id, season_id):
    with store.transaction():
        store.delete_season(season_id)          # clear the dependent Season first
        store.delete_program(program_id)


def _mutation(store, program_id, season_id, kind):
    """A concurrent-style commit against the selected context: archive/reopen the
    Season, delete it, or delete its whole Program (Season first)."""
    return {
        "archive": lambda: _archive(store, season_id),
        "reopen": lambda: _reopen(store, season_id),
        "delete_season": lambda: store.delete_season(season_id),
        "delete_program": lambda: _delete_program_cascade(
            store, program_id, season_id),
    }[kind]


_MUTATIONS = ("archive", "reopen", "delete_season", "delete_program")


def _assert_context_consistent(t, c, label):
    """The rendered context payload is internally consistent: a non-null id
    always has its serialized object (ids never dangle) and ``read_only`` always
    agrees with the EXACT serialized Season status (#159 snapshot contract)."""
    t.assertNotIn("error", c, (label, c))
    if c["program_id"] is None:
        t.assertIsNone(c["program"], (label, c))
    else:
        t.assertIsNotNone(c["program"], (label, c))          # id never dangles
        t.assertEqual(c["program"]["id"], c["program_id"], (label, c))
    if c["season_id"] is None:
        t.assertIsNone(c["season"], (label, c))
        t.assertFalse(c["read_only"], (label, c))            # no Season ⇒ writable
    else:
        t.assertIsNotNone(c["season"], (label, c))           # id never dangles
        t.assertEqual(c["season"]["id"], c["season_id"], (label, c))
        t.assertEqual(                                       # read_only ⟺ status
            c["read_only"],
            c["season"]["status"] == SeasonStatus.ARCHIVED.value, (label, c))
    # The League axis (#345, wired by #360) obeys the same snapshot rule: it is
    # resolved inside the SAME transaction as the other two and rendered from
    # the object that was validated, so its id can never dangle either. A null
    # League is legitimate (Program-only, Season-without-League, or a League the
    # concurrent mutation invalidated), so only the pairing is asserted.
    if c["league_id"] is None:
        t.assertIsNone(c["league"], (label, c))
    else:
        t.assertIsNotNone(c["league"], (label, c))
        t.assertEqual(c["league"]["id"], c["league_id"], (label, c))


class ContextResolveSetTest(unittest.TestCase):
    """resolve/set behavior for a global operator (role/scope threaded)."""

    def test_empty_world_is_empty_context(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                # The empty context carries every axis as null — including the
                # League added by #360. Asserted as the WHOLE dict on purpose:
                # an unexpected extra key here would be a contract leak.
                self.assertEqual(
                    api.get_active_context("u1", *ADMIN),
                    {"program_id": None, "season_id": None, "league_id": None,
                     "read_only": False,
                     "program": None, "season": None, "league": None}, label)
                _close(store)

    def test_fallback_prefers_latest_active_season_semantically(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]
                # Insert out of date order; a null-date season must NOT win over a
                # dated one, and the LATEST date wins (not string/insertion order).
                s_mid = api.create_season(pid, "mid", start_date="2027-06-01")["id"]
                s_late = api.create_season(pid, "late", start_date="2027-09-01")["id"]
                api.create_season(pid, "nodate")           # null start_date
                api.create_season(pid, "early", start_date="2027-01-01")
                c = api.get_active_context("u1", *ADMIN)
                self.assertEqual(c["season_id"], s_late, (label, c))
                self.assertFalse(c["read_only"], label)
                self.assertNotEqual(c["season_id"], s_mid, label)
                _close(store)

    def test_program_only_when_no_active_season(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]   # no Season yet
                c = api.get_active_context("u1", *ADMIN)
                self.assertEqual(c["program_id"], pid, label)      # Program-only
                self.assertIsNone(c["season_id"], label)
                # Explicit Program-only set round-trips.
                c = api.set_active_context("u1", *ADMIN, pid, None)
                self.assertEqual((c["program_id"], c["season_id"]), (pid, None),
                                 label)
                self.assertFalse(c["read_only"], label)
                _close(store)

    def test_archived_selection_is_read_only_history_not_replaced(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                _archive(store, s2)
                # Setting an archived Season is allowed — a read-only historical
                # context — NOT rejected, NOT swapped for p1's active Season.
                c = api.set_active_context("u1", *ADMIN, p2, s2)
                self.assertEqual(c["season_id"], s2, (label, c))
                self.assertTrue(c["read_only"], (label, c))
                self.assertEqual(api.get_active_context("u1", *ADMIN)["season_id"],
                                 s2, label)                        # honored on resolve
                _close(store)

    def test_deleted_season_and_deleted_program_fall_back(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                # deleted Season → fallback
                api.set_active_context("u1", *ADMIN, p2, s2)
                store.delete_season(s2)
                self.assertNotEqual(
                    api.get_active_context("u1", *ADMIN)["season_id"], s2, label)
                # deleted Program → fallback (never a dangling program_id)
                api.set_active_context("u2", *ADMIN, p1, s1)
                with store.transaction():
                    store.delete_season(s1)          # clear the dependent first
                    store.delete_program(p1)
                c = api.get_active_context("u2", *ADMIN)
                self.assertNotEqual(c["program_id"], p1, (label, c))
                _close(store)

    def test_orphan_row_and_per_user_isolation(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                p2, s2 = _program_season(api, "P2", "S2")
                api.set_active_context("u1", *ADMIN, p2, s2)
                # u2 has no saved row ⇒ deterministic fallback, not u1's choice.
                self.assertEqual(
                    api.get_active_context("u2", *ADMIN)["program_id"], p1, label)
                # An orphan row (user has no account) is inert: resolving it just
                # applies the passed role/scope; no crash.
                store.set_active_context(ActiveContext(
                    "ghost_user", p2, s2, datetime(2027, 1, 1, tzinfo=timezone.utc)))
                self.assertEqual(
                    api.get_active_context("ghost_user", *ADMIN)["season_id"], s2,
                    label)
                _close(store)


class ContextAuthorizationMatrixTest(unittest.TestCase):
    """resolve/set are filtered through the caller's REAL role + scope."""

    def test_global_roles_see_all_programs(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                for role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
                    c = api.get_active_context(f"u_{role.value}", role, {})
                    self.assertEqual(c["program_id"], p1, (label, role))
                    ok = api.set_active_context(f"u_{role.value}", role, {}, p1, s1)
                    self.assertEqual(ok["season_id"], s1, (label, role))
                _close(store)

    def test_unknown_or_unbound_role_fails_closed(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                # A Coach with no team_id, an Official with no official_id: empty
                # authorized set ⇒ empty context, and a set is a non-oracle 404.
                for role, scope in ((Role.COACH, {}), (Role.OFFICIAL, {}),
                                    (Role.GUARDIAN, {})):
                    self.assertIsNone(
                        api.get_active_context("x", role, scope)["program_id"],
                        (label, role))
                    r = api.set_active_context("x", role, scope, p1, s1)
                    self.assertEqual(r["error"]["code"], "not_found", (label, role))
                _close(store)

    def test_coach_scoped_to_own_program_only(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                p2, s2 = _program_season(api, "P2", "S2")   # unrelated
                coach = (Role.COACH, {"team_id": team})
                c = api.get_active_context("c1", *coach)
                self.assertEqual(c["program_id"], p1, (label, c))   # only own program
                self.assertEqual(c["season_id"], s1, (label, c))
                # Selecting the unrelated Program is a non-oracle not_found.
                r = api.set_active_context("c1", *coach, p2, s2)
                self.assertEqual(r["error"]["code"], "not_found", label)
                self.assertEqual(r["error"]["details"]["reason"],
                                 "program_not_accessible", label)
                # Its own is allowed.
                self.assertEqual(
                    api.set_active_context("c1", *coach, p1, s1)["season_id"], s1,
                    label)
                _close(store)

    def test_player_live_team_transfer_deactivate_teamless(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team1 = _team_registered(api, s1, "T1")
                p2, s2 = _program_season(api, "P2", "S2")
                team2 = _team_registered(api, s2, "T2")
                player = api.create_player(team1, "Pat", "forward")["id"]
                pl = (Role.PLAYER, {"player_id": player})
                # Live resolution: player sees team1's Program only.
                self.assertEqual(api.get_active_context("pl", *pl)["program_id"],
                                 p1, label)
                # Transfer to team2 (different Program) → former Program rejected,
                # new one granted — resolved LIVE from player_id.
                pobj = store.get_player(player)
                pobj.team_id = team2
                store.save_player(pobj)
                self.assertEqual(api.get_active_context("pl", *pl)["program_id"],
                                 p2, label)
                self.assertEqual(api.set_active_context("pl", *pl, p1, s1)
                                 ["error"]["code"], "not_found", label)  # former
                # Deactivated player → fails closed (empty).
                pobj = store.get_player(player)
                pobj.is_active = False
                store.save_player(pobj)
                self.assertIsNone(api.get_active_context("pl", *pl)["program_id"],
                                  label)
                # Reactivated but teamless (no team at all) → still closed.
                pobj = store.get_player(player)
                pobj.is_active = True
                pobj.team_id = None
                store.save_player(pobj)
                self.assertIsNone(api.get_active_context("pl", *pl)["program_id"],
                                  label)
                # Deleted player row → closed (no live subject to resolve).
                store.delete_player(player)
                self.assertIsNone(api.get_active_context("pl", *pl)["program_id"],
                                  label)
                _close(store)

    def test_official_sees_only_assigned_season_and_loses_it_on_unassign(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                p2, s2 = _program_season(api, "P2", "S2")     # unassigned → unseen
                oid, gid = _official_assigned(api, s1, team)
                off = (Role.OFFICIAL, {"official_id": oid})
                c = api.get_active_context("o1", *off)
                self.assertEqual(c["program_id"], p1, (label, c))  # assigned only
                self.assertEqual(api.set_active_context("o1", *off, p2, s2)
                                 ["error"]["code"], "not_found", label)  # unrelated
                # Removing the assignment removes access immediately.
                with store.transaction():
                    store.remove_official_assignment(
                        store.assignments_for_official(oid)[0].id)
                self.assertIsNone(api.get_active_context("o1", *off)["program_id"],
                                  label)
                _close(store)

    def test_guardian_verified_link_only(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                team = _team_registered(api, s1)
                junior = api.create_player(team, "Junior", "forward")["id"]
                ts = datetime(2027, 1, 1, tzinfo=timezone.utc)
                with store.transaction():   # link rows directly (context reads them)
                    store.add_guardian_link(GuardianLink(
                        id="gl_unv", guardian_user_id="g_unv", player_id=junior,
                        created_at=ts, verified=False))
                    store.add_guardian_link(GuardianLink(
                        id="gl_ver", guardian_user_id="g_ver", player_id=junior,
                        created_at=ts, verified=True))
                # Unverified link grants nothing; a verified link grants the
                # junior's authorized Program/Season.
                self.assertIsNone(
                    api.get_active_context("g_unv", Role.GUARDIAN, {})["program_id"],
                    label)
                self.assertEqual(
                    api.get_active_context("g_ver", Role.GUARDIAN, {})["program_id"],
                    p1, label)
                # Junior transfer to a team in a different Program → the guardian's
                # authorized context refilters LIVE to the new Program.
                p2, s2 = _program_season(api, "P2", "S2")
                team2 = _team_registered(api, s2, "T2")
                jobj = store.get_player(junior)
                jobj.team_id = team2
                store.save_player(jobj)
                self.assertEqual(
                    api.get_active_context("g_ver", Role.GUARDIAN, {})["program_id"],
                    p2, label)
                # Revoking the link (verified → False) removes all access.
                link = store.guardian_links_for("g_ver")[0]
                link.verified = False
                store.save_guardian_link(link)
                self.assertIsNone(
                    api.get_active_context("g_ver", Role.GUARDIAN, {})["program_id"],
                    label)
                _close(store)

    def test_transfer_drops_prior_league_season_but_preserves_history(self):
        # DECIDED behavior (#159 follow-up deliberately deferred, per the review
        # on PR #307): a scoped Coach / live Player / verified Guardian sees the
        # Seasons its Team actively participates in under its CURRENT league. A
        # same-league ARCHIVED Season stays SELECTABLE read-only; but once the
        # Team TRANSFERS to a new league (#283 same-Program rule), the prior
        # league's frozen Season leaves the scoped entitlement — while the
        # registration AND its Game/result history are preserved byte-for-byte.
        # Only the VIEW entitlement narrows; widening it to prior-Team history is
        # a separate, security-sensitive slice. Proven on Memory/SQLite/PostgreSQL.
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                org = api.create_organization("Org", "O")["id"]
                pid = api.create_program(
                    "P1", "US", "UTC", operator_organization_id=org)["id"]
                s1 = api.create_season(pid, "S1")["id"]
                la = api.create_league(s1, "LA")["id"]
                da = api.create_division(s1, "DA", league_id=la)["id"]
                club = api.create_club("Club")["id"]
                alpha = api.create_team(club_id=club, name="Alpha",
                                        league_id=la, division_id=da)["id"]
                bravo = api.create_team(club_id=club, name="Bravo",
                                        league_id=la, division_id=da)["id"]
                api.setup.register_team_for_season(s1, alpha, da, league_id=la)
                api.setup.register_team_for_season(s1, bravo, da, league_id=la)
                # A representative Game + FINAL result under S1 — the frozen
                # history that must survive the transfer byte-for-byte.
                venue = api.create_venue("V", organization_id=org,
                                         league_id=pid)["id"]
                api.grant_season_venue_access(s1, venue)
                rink = api.create_rink(venue, "R")["id"]
                t0 = datetime(2027, 3, 1, 18, tzinfo=timezone.utc)
                slot = api.create_ice_slot(
                    rink, t0.isoformat(),
                    t0.replace(hour=20).isoformat(), "game")["id"]
                gid = api.create_game(s1, da, alpha, bravo, slot,
                                      league_id=la)["id"]
                api.record_result(gid, 4, 1)
                api.approve_result(gid)
                la_ls = store.league_seasons_for_league(la)[0].id
                reg_id = store.registration_for_team_in_league_season(
                    la_ls, alpha).id
                before = {
                    "reg": asdict(store.get_season_team_registration(reg_id)),
                    "game": asdict(store.get_game(gid)),
                    "result": asdict(store.result_for_game(gid)),
                }
                coach = (Role.COACH, {"team_id": alpha})
                _archive(store, s1)
                # Same-league archived history IS selectable read-only (retained).
                r = api.set_active_context("coach", *coach, pid, s1)
                self.assertEqual(r["season_id"], s1, (label, r))
                self.assertTrue(r["read_only"], (label, r))
                # Transfer Alpha to a new League LB (same Program) in S2. #283
                # freezes the archived S1/LA registration (never moved).
                s2 = api.create_season(pid, "S2")["id"]
                lb = api.create_league(s2, "LB")["id"]
                db = api.create_division(s2, "DB", league_id=lb)["id"]
                moved = api.transfer_team_to_league(alpha, lb)
                self.assertNotIn("error", moved, (label, moved))
                api.setup.register_team_for_season(s2, alpha, db, league_id=lb)
                player = api.create_player(alpha, "Pat", "forward")["id"]
                junior = api.create_player(alpha, "Jun", "forward")["id"]
                ts = datetime(2027, 1, 1, tzinfo=timezone.utc)
                with store.transaction():
                    store.add_guardian_link(GuardianLink(
                        id="gl_t", guardian_user_id="guard", player_id=junior,
                        created_at=ts, verified=True))
                subjects = (("coach", coach), ("player",
                            (Role.PLAYER, {"player_id": player})),
                            ("guard", (Role.GUARDIAN, {})))
                for uid, rs in subjects:
                    cc = api.get_active_context(uid, *rs)
                    # Same Program; fallback prefers the CURRENT active Season S2.
                    self.assertEqual(cc["program_id"], pid, (label, uid, cc))
                    self.assertEqual(cc["season_id"], s2, (label, uid, cc))
                    self.assertFalse(cc["read_only"], (label, uid, cc))
                    # The prior-league S1 is no longer selectable (decided loss),
                    # as a non-oracle not_found; the current S2 still is.
                    self.assertEqual(
                        api.set_active_context(uid, *rs, pid, s1)["error"]["code"],
                        "not_found", (label, uid))
                    self.assertEqual(
                        api.set_active_context(uid, *rs, pid, s2)["season_id"],
                        s2, (label, uid))
                # An unrelated identity is still denied the whole Program.
                self.assertIsNone(api.get_active_context(
                    "stranger", Role.COACH, {"team_id": "ghost"})["program_id"],
                    label)
                # History is byte-identical: the frozen registration, its Game,
                # and its result are all unchanged by the transfer.
                after = {
                    "reg": asdict(store.get_season_team_registration(reg_id)),
                    "game": asdict(store.get_game(gid)),
                    "result": asdict(store.result_for_game(gid)),
                }
                self.assertEqual(after, before, label)
                _close(store)

    def test_genuinely_unknown_role_fails_closed(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                # A role value OUTSIDE the known set (not a handled Role) must hit
                # the fail-closed default — empty authorized set, empty context,
                # and a non-oracle not_found on set. Proves the default branch.
                for unknown in (None, "superuser"):
                    self.assertEqual(context_scope.authorized_program_ids(
                        store, unknown, {}, "u"), set(), (label, unknown))
                    self.assertIsNone(
                        api.get_active_context("u", unknown, {})["program_id"],
                        (label, unknown))
                    r = api.set_active_context("u", unknown, {}, p1, s1)
                    self.assertEqual(r["error"]["code"], "not_found",
                                     (label, unknown))
                _close(store)


class ContextOptionsTest(unittest.TestCase):
    """GET /api/context/options enumerates ONLY the caller's authorized
    Programs/Seasons (the SAME rules as get/set — never the unfiltered overview),
    each Program carrying its authorized Seasons with archived-read-only metadata,
    and a ``selected`` that is always one of the offered options."""

    def _ids(self, opts):
        return {p["id"]: {s["id"] for s in p["seasons"]}
                for p in opts["programs"]}

    def test_global_operator_sees_all_programs_and_seasons(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                s1b = api.create_season(p1, "S1b")["id"]
                p2, s2 = _program_season(api, "P2", "S2")
                _archive(store, s1b)
                o = api.get_context_options("u", *ADMIN)
                self.assertNotIn("error", o, o)
                ids = self._ids(o)
                self.assertEqual(set(ids), {p1, p2}, (label, ids))
                self.assertEqual(ids[p1], {s1, s1b}, label)
                arch = [s for pr in o["programs"] if pr["id"] == p1
                        for s in pr["seasons"] if s["id"] == s1b][0]
                self.assertTrue(arch["read_only"], arch)         # archived → RO
                self.assertEqual(arch["status"], "archived", arch)
                self.assertIn(o["selected"]["program_id"], ids, o)  # within options
                _close(store)

    def test_coach_sees_only_own_program_and_participating_seasons(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                p1, s1 = _program_season(api, "P1", "S1")
                # a second Season under the SAME program the team does NOT join
                s1_other = api.create_season(p1, "S1-other")["id"]
                team = _team_registered(api, s1)
                _program_season(api, "P2", "S2")                 # unrelated program
                o = api.get_context_options("c", Role.COACH, {"team_id": team})
                ids = self._ids(o)
                self.assertEqual(set(ids), {p1}, (label, ids))   # own program only
                self.assertEqual(ids[p1], {s1}, label)           # participating only
                self.assertNotIn(s1_other, ids[p1], label)       # non-participating out
                _close(store)

    def test_unbound_role_gets_empty_options(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                _program_season(api, "P1", "S1")
                o = api.get_context_options("x", Role.COACH, {})
                self.assertEqual(o["programs"], [], (label, o))
                self.assertEqual(
                    o["selected"],
                    {"program_id": None, "season_id": None, "league_id": None,
                     "read_only": False},
                    o)
                _close(store)

    def test_program_only_program_is_offered_with_no_seasons(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid = api.create_program("P", "US", "UTC")["id"]  # no Season yet
                o = api.get_context_options("u", *ADMIN)
                self.assertEqual(self._ids(o), {pid: set()}, (label, o))
                self.assertEqual(o["selected"]["program_id"], pid, o)
                self.assertIsNone(o["selected"]["season_id"], o)  # Program-only
                _close(store)


class ContextSubjectContractTest(unittest.TestCase):
    """The context selector and the web scope guards resolve the SAME caller
    identity (one shared resolver), after transfer and deactivate — proving the
    de-duplication holds, not just today's happy path."""

    def test_context_and_web_agree_on_team_after_transfer_and_deactivate(self):
        store = InMemoryStore()
        api = ApiService(store)
        p1, s1 = _program_season(api, "P1", "S1")
        team1 = _team_registered(api, s1, "T1")
        p2, s2 = _program_season(api, "P2", "S2")
        team2 = _team_registered(api, s2, "T2")
        player = api.create_player(team1, "Pat", "forward")["id"]
        scope = {"player_id": player}
        # Both gates resolve the same live team, and context's Program set matches.
        self.assertEqual(own_team_id(Role.PLAYER, scope, store), team1)
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            {p1})
        # After a transfer, both still agree (same shared resolver).
        pobj = store.get_player(player); pobj.team_id = team2; store.save_player(pobj)
        self.assertEqual(own_team_id(Role.PLAYER, scope, store), team2)
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            {p2})
        # After deactivate, both fail closed identically.
        pobj = store.get_player(player); pobj.is_active = False; store.save_player(pobj)
        self.assertIsNone(own_team_id(Role.PLAYER, scope, store))
        self.assertEqual(
            context_scope.authorized_program_ids(store, Role.PLAYER, scope, "pl"),
            set())

    def test_stale_saved_selection_is_ignored_not_rewritten(self):
        # A saved selection whose Program the caller is no longer authorized for
        # is IGNORED (fallback) but NOT rewritten, so restoring authorization
        # restores the choice.
        store = InMemoryStore()
        api = ApiService(store)
        p1, s1 = _program_season(api, "P1", "S1")
        team = _team_registered(api, s1)
        p2, s2 = _program_season(api, "P2", "S2")
        api.set_active_context("u", *ADMIN, p2, s2)          # admin saves p2
        coach = (Role.COACH, {"team_id": team})              # coach can't see p2
        self.assertEqual(api.get_active_context("u", *coach)["program_id"], p1)
        self.assertEqual(store.get_active_context("u").program_id, p2)  # not rewritten
        self.assertEqual(api.get_active_context("u", *ADMIN)["season_id"], s2)  # reappears


class ContextSnapshotConsistencyTest(unittest.TestCase):
    """Validation and response rendering read ONE authoritative snapshot: a
    concurrent archive / reopen / Season-delete / Program-delete landing in the
    validate→render window can never make the payload self-contradict. The fix
    returns the exact validated Program/Season objects from ``resolve``/``set``
    and renders those (deriving ``read_only`` from the serialized Season), so a
    naive re-fetch can no longer disagree. Deterministic Memory/SQLite parity:
    the mutation is injected at the precise boundary between validation and
    rendering (this would fail the old scalar-id + re-fetch implementation)."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def _mutate_after(self, api, method_name, mutate):
        """Land ``mutate`` right after the validated snapshot is produced — the
        exact window a re-fetch would have observed a concurrent commit.

        Returns a ``fired`` dict the caller MUST assert on. Without that check
        this helper fails open: if the facade stops calling ``method_name`` the
        wrapper simply never runs, the mutation never lands, and the assertions
        below all pass while proving nothing. That is exactly what happened when
        #360 moved ``get``/``set_active_context`` onto the ``*_with_league``
        entry points — the suite stayed green on a test that had gone vacuous.
        A silently-vacuous race test is worse than a failing one, so the
        non-vacuity check is part of the helper's contract now."""
        orig = getattr(api.context, method_name)
        fired = {"done": False}

        def wrapper(*a, **k):
            r = orig(*a, **k)          # validated snapshot; its transaction closed
            fired["done"] = True
            mutate()                   # concurrent-style commit lands NOW
            return r

        setattr(api.context, method_name, wrapper)
        return fired

    def test_get_is_snapshot_consistent_under_concurrent_mutation(self):
        for kind in _MUTATIONS:
            for label, store in self._stores():
                with self.subTest(mutation=kind, backend=label):
                    api = ApiService(store)
                    pid, sid = _program_season(api, "P1", "S1")
                    if kind == "reopen":
                        _archive(store, sid)               # start archived → reopen
                    api.set_active_context("u", *ADMIN, pid, sid)
                    fired = self._mutate_after(
                        api, "resolve_with_league",
                        _mutation(store, pid, sid, kind))
                    _assert_context_consistent(
                        self, api.get_active_context("u", *ADMIN), (kind, label))
                    self.assertTrue(fired["done"], (kind, label))  # not vacuous
                    _close(store)

    def test_post_is_snapshot_consistent_and_grants_no_authority(self):
        for kind in _MUTATIONS:
            for label, store in self._stores():
                with self.subTest(mutation=kind, backend=label):
                    api = ApiService(store)
                    pid, sid = _program_season(api, "P1", "S1")
                    if kind == "reopen":
                        _archive(store, sid)
                    fired = self._mutate_after(
                        api, "set_with_league",
                        _mutation(store, pid, sid, kind))
                    c = api.set_active_context("u", *ADMIN, pid, sid)
                    _assert_context_consistent(self, c, (kind, label))
                    self.assertTrue(fired["done"], (kind, label))  # not vacuous
                    # The stored preference is NOT rewritten by the race (it still
                    # records the user's exact choice — a dangling row grants no
                    # authority), and a later resolve renders a consistent view.
                    saved = store.get_active_context("u")
                    self.assertEqual((saved.program_id, saved.season_id),
                                     (pid, sid), (kind, label))
                    _assert_context_consistent(
                        self, api.get_active_context("u", *ADMIN),
                        (kind, label, "after"))
                    _close(store)

    def test_render_uses_the_validated_snapshot_not_a_concurrent_inplace_edit(self):
        # InMemoryStore hands back a live, shared, mutable Season. If a concurrent
        # archive/reopen mutates that row *while* _context_view is serializing it
        # (after read_only would be captured, before/inside serialization), the
        # payload must still reflect the value the request VALIDATED — a detached
        # snapshot — never a torn mid-render mix (read_only disagreeing with the
        # serialized status). Barrier: fire the in-place edit against the store's
        # LIVE row at the instant the Season is about to be serialized.
        import hockey_scheduler.api.service as svc
        cases = (("archive", SeasonStatus.ACTIVE.value, False),
                 ("reopen", SeasonStatus.ARCHIVED.value, True))
        for kind, expect_status, expect_ro in cases:
            for label, store in self._stores():
                with self.subTest(mutation=kind, backend=label):
                    api = ApiService(store)
                    pid, sid = _program_season(api, "P1", "S1")
                    if kind == "reopen":
                        _archive(store, sid)           # validated state = archived
                    api.set_active_context("u", *ADMIN, pid, sid)
                    orig_serialize = svc._serialize
                    fired = {"done": False}

                    def racing_serialize(obj):
                        if not fired["done"] and getattr(obj, "id", None) == sid:
                            fired["done"] = True       # concurrent in-place edit
                            _mutation(store, pid, sid, kind)()   # hits LIVE row
                        return orig_serialize(obj)

                    svc._serialize = racing_serialize
                    try:
                        c = api.get_active_context("u", *ADMIN)
                    finally:
                        svc._serialize = orig_serialize
                    self.assertTrue(fired["done"], (kind, label))  # race did fire
                    _assert_context_consistent(self, c, (kind, label))
                    # ...and the rendered value is the VALIDATED snapshot, proving
                    # the mid-render live edit never reached the response object.
                    self.assertEqual(c["season"]["status"], expect_status,
                                     (kind, label, c))
                    self.assertEqual(c["read_only"], expect_ro, (kind, label))
                    _close(store)


class ContextUpsertParityTest(unittest.TestCase):
    """set_active_context is last-write-wins on every backend: a repeat is
    idempotent (one row), and two threads racing one user leave exactly one row
    with no error — the Memory/SQLite parity for the atomic SQL upsert."""

    _TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

    def _row_count(self, store):
        if isinstance(store, SqlStore):
            cur = store.conn.cursor()   # sqlite3 cursor isn't a context manager
            cur.execute("SELECT COUNT(*) AS c FROM user_active_context")
            return cur.fetchone()["c"]
        return len(store.user_active_context)

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_sequential_repeat_is_one_row_last_wins(self):
        for label, store in self._stores():
            with self.subTest(backend=label):
                store.set_active_context(ActiveContext("u", "pA", "sA", self._TS))
                store.set_active_context(ActiveContext("u", "pB", "sB", self._TS))
                self.assertEqual(self._row_count(store), 1, label)
                self.assertEqual(store.get_active_context("u").program_id, "pB",
                                 label)
                _close(store)

    def test_concurrent_writes_leave_exactly_one_row(self):
        for label, store in self._stores():
            with self.subTest(backend=label):
                barrier = threading.Barrier(2)
                errs = []

                def w(program):
                    try:
                        barrier.wait()
                        store.set_active_context(
                            ActiveContext("u", program, "s", self._TS))
                    except Exception as exc:
                        errs.append(f"{program}:{exc}")

                threads = [threading.Thread(target=w, args=(p,))
                           for p in ("pX", "pY")]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
                self.assertEqual(errs, [], (label, errs))            # no error
                self.assertEqual(self._row_count(store), 1, label)   # one row
                self.assertIn(store.get_active_context("u").program_id,
                              ("pX", "pY"), label)
                _close(store)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ContextConcurrencyPgTest(unittest.TestCase):
    """Two independent PostgreSQL connections write one user's context with a
    CONTROLLED release order, so the last committer is deterministic: the write
    that is released FIRST commits first, and the one that was blocked commits
    LAST and owns the final value. Both succeed (no 500) and exactly one row
    remains — for a first write and for an existing row."""

    _TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_until_blocked_on_lock(self, backend_pid, timeout=10.0):
        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT state, wait_event_type FROM pg_stat_activity "
                        "WHERE pid = %s", (backend_pid,))
                    row = cur.fetchone()
                    if (row is not None and row[0] == "active"
                            and row[1] == "Lock"):
                        return True
                time.sleep(0.02)
        return False

    def _controlled_race(self, seed):
        if seed:
            SqlStore(self.url).set_active_context(
                ActiveContext("u", "pInit", "sInit", self._TS))
        first = SqlStore(self.url)   # released FIRST → commits first
        last = SqlStore(self.url)    # blocks on first's lock → commits LAST
        orig = first._exec
        wrote = threading.Event()
        release = threading.Event()

        def instrumented(query, params=()):
            result = orig(query, params)
            if "user_active_context" in query:   # after the write, txn still open
                wrote.set()
                release.wait(15)
            return result

        first._exec = instrumented
        results = {}

        def write(store, key, program):
            try:
                store.set_active_context(
                    ActiveContext("u", program, "s", self._TS))
                results[key] = "ok"
            except Exception as exc:
                results[key] = f"ERR:{exc}"

        t_first = threading.Thread(
            target=lambda: write(first, "first", "pFirst"))
        t_last = threading.Thread(target=lambda: write(last, "last", "pLast"))
        last_pid = self._backend_pid(last)
        t_first.start()
        self.assertTrue(wrote.wait(15), "first write never reached its INSERT")
        t_last.start()
        self.assertTrue(self._wait_until_blocked_on_lock(last_pid),
                        "second write never blocked on the first's row lock")
        release.set()                # first commits, THEN last proceeds & commits
        t_first.join(20)
        t_last.join(20)
        first.close()
        last.close()
        return results

    def _assert_last_wins(self, results):
        self.assertEqual(results, {"first": "ok", "last": "ok"}, results)  # 200
        check = SqlStore(self.url)
        with check.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM user_active_context "
                        "WHERE id = 'u'")
            self.assertEqual(cur.fetchone()["c"], 1)                # one row
        self.assertEqual(check.get_active_context("u").program_id,
                         "pLast")                                   # last wins
        check.close()

    def test_first_write_race_last_commit_wins(self):
        self._assert_last_wins(self._controlled_race(seed=False))

    def test_existing_row_race_last_commit_wins(self):
        self._assert_last_wins(self._controlled_race(seed=True))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ContextSnapshotConsistencyPgTest(unittest.TestCase):
    """Real two-connection PostgreSQL barrier for the snapshot contract:
    connection A validates a context then PAUSES between validation and
    rendering while connection B commits a concurrent archive / reopen / Season
    delete / deletable-Program delete. A then renders. Because A renders the
    exact objects it validated (never a re-fetch), the payload is internally
    consistent every time — ``read_only`` agrees with the serialized Season
    status and no id dangles — for both GET and POST."""

    _TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _seed(self, archived):
        store = SqlStore(self.url)
        api = ApiService(store)
        pid, sid = _program_season(api, "P1", "S1")
        if archived:
            _archive(store, sid)
        store.set_active_context(ActiveContext("u", pid, sid, self._TS))
        store.close()
        return pid, sid

    def _race(self, verb, kind):
        pid, sid = self._seed(archived=(kind == "reopen"))
        reader = SqlStore(self.url)
        mutator = SqlStore(self.url)
        api = ApiService(reader)
        paused = threading.Event()
        release = threading.Event()
        # Patch the seam the FACADE actually calls. Since #360 wired the League
        # axis to HTTP, get/set_active_context go through the *_with_league
        # entry points; patching the two-axis `resolve`/`set` would silently
        # never fire, and this barrier would hang rather than fail loudly about
        # the real cause. Pausing here still means exactly what it always did:
        # after the service has validated and DETACHED its objects (its
        # transaction closed), before the facade renders them.
        method = "resolve_with_league" if verb == "GET" else "set_with_league"
        orig = getattr(api.context, method)

        def paused_boundary(*a, **k):
            r = orig(*a, **k)          # validated snapshot; its transaction closed
            paused.set()
            release.wait(15)
            return r

        setattr(api.context, method, paused_boundary)
        out = {}

        def run():
            if verb == "GET":
                out["c"] = api.get_active_context("u", *ADMIN)
            else:
                out["c"] = api.set_active_context("u", *ADMIN, pid, sid)

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(paused.wait(15), (verb, kind))
        _mutation(mutator, pid, sid, kind)()    # committed on connection B
        release.set()
        t.join(20)
        reader.close()
        mutator.close()
        return out["c"]

    def test_get_races_are_snapshot_consistent(self):
        for kind in _MUTATIONS:
            with self.subTest(verb="GET", mutation=kind):
                _assert_context_consistent(self, self._race("GET", kind), kind)

    def test_post_races_are_snapshot_consistent(self):
        for kind in _MUTATIONS:
            with self.subTest(verb="POST", mutation=kind):
                _assert_context_consistent(self, self._race("POST", kind), kind)


def _pause_after_program_snapshot():
    """Patch context_scope.authorized_program_ids so the FIRST caller pauses
    right after it returns — i.e. after its authorization snapshot is fixed but
    before Seasons are read. Returns (paused, release, restore)."""
    cs = context_scope
    orig = cs.authorized_program_ids
    paused = threading.Event()
    release = threading.Event()
    armed = {"v": True}

    def barrier(*a, **k):
        r = orig(*a, **k)
        if armed["v"]:
            armed["v"] = False
            paused.set()
            release.wait(20)
        return r

    cs.authorized_program_ids = barrier
    return paused, release, (lambda: setattr(cs, "authorized_program_ids", orig))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class ContextAuthorizationRacePgTest(unittest.TestCase):
    """The whole authorization + selection reads ONE SERIALIZABLE snapshot, so a
    scope revocation that COMMITS mid-request can never produce a hybrid
    former-Program/empty-Season result: the request resolves wholly to the
    pre-change scope (authorized) OR wholly to the post-change scope (denied),
    never a mix, and errors stay non-oracle. Forced PostgreSQL: two independent
    connections (reader + revoker); the reader pauses after its authorized-Program
    snapshot is fixed while the revoker commits, then resumes."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _run_race(self, build, reader_call, revoker):
        seed = SqlStore(self.url)
        world = build(ApiService(seed))
        seed.close()
        reader = SqlStore(self.url)
        rapi = ApiService(reader)
        mutator = SqlStore(self.url)
        mapi = ApiService(mutator)
        paused, release, restore = _pause_after_program_snapshot()
        out = {}

        def run():
            try:
                out["c"] = reader_call(rapi, world)
            except Exception as exc:                       # noqa: BLE001
                out["err"] = repr(exc)

        t = threading.Thread(target=run)
        try:
            t.start()
            self.assertTrue(paused.wait(20), "reader never fixed its snapshot")
            revoker(mapi, mutator, world)                  # commits on connection B
            release.set()
            t.join(30)
        finally:
            restore()
        reader.close()
        mutator.close()
        self.assertNotIn("err", out, out)
        return out["c"], world

    def _assert_between(self, c, pre, post):
        """The rendered context is EXACTLY one of the two consistent snapshots —
        wholly pre-change or wholly post-change — never the hybrid in between."""
        self.assertNotIn("error", c, c)
        got = (c["program_id"], c["season_id"])
        self.assertIn(got, (pre, post),
                      ("hybrid or unexpected authorization result", got, pre, post))

    # -- Official assignment ------------------------------------------------
    def _official_world(self, api):
        pid, sid = _program_season(api, "P", "S")
        team = _team_registered(api, sid)
        oid, gid = _official_assigned(api, sid, team)
        return {"pid": pid, "sid": sid, "oid": oid}

    def _unassign(self, mapi, mstore, w):
        with mstore.transaction():
            mstore.remove_official_assignment(
                mstore.assignments_for_official(w["oid"])[0].id)

    def test_official_unassign_vs_get_is_wholly_pre_or_post(self):
        c, w = self._run_race(
            self._official_world,
            lambda api, w: api.get_active_context(
                "o", Role.OFFICIAL, {"official_id": w["oid"]}),
            self._unassign)
        self._assert_between(c, (w["pid"], w["sid"]), (None, None))

    def test_program_only_post_vs_unassign_is_wholly_pre_or_post(self):
        c, w = self._run_race(
            self._official_world,
            lambda api, w: api.set_active_context(
                "o", Role.OFFICIAL, {"official_id": w["oid"]}, w["pid"], None),
            self._unassign)
        if "error" in c:                                   # post-change: denied
            self.assertEqual(c["error"]["code"], "not_found", c)
            self.assertEqual(c["error"]["details"]["reason"],
                             "program_not_accessible", c)
        else:                                              # pre-change: persisted
            self.assertEqual((c["program_id"], c["season_id"]), (w["pid"], None), c)

    # -- Player -------------------------------------------------------------
    def test_player_deactivate_vs_get_is_wholly_pre_or_post(self):
        def build(api):
            pid, sid = _program_season(api, "P", "S")
            team = _team_registered(api, sid)
            player = api.create_player(team, "Pat", "forward")["id"]
            return {"pid": pid, "sid": sid, "player": player}

        def deactivate(mapi, mstore, w):
            with mstore.transaction():
                p = mstore.get_player(w["player"])
                p.is_active = False
                mstore.save_player(p)

        c, w = self._run_race(
            build,
            lambda api, w: api.get_active_context(
                "pl", Role.PLAYER, {"player_id": w["player"]}),
            deactivate)
        self._assert_between(c, (w["pid"], w["sid"]), (None, None))

    def test_player_transfer_vs_get_is_wholly_pre_or_post(self):
        def build(api):
            p1, s1 = _program_season(api, "P1", "S1")
            t1 = _team_registered(api, s1, "T1")
            p2, s2 = _program_season(api, "P2", "S2")
            t2 = _team_registered(api, s2, "T2")
            player = api.create_player(t1, "Pat", "forward")["id"]
            return {"p1": p1, "s1": s1, "p2": p2, "s2": s2, "t2": t2,
                    "player": player}

        def transfer(mapi, mstore, w):
            with mstore.transaction():
                p = mstore.get_player(w["player"])
                p.team_id = w["t2"]
                mstore.save_player(p)

        c, w = self._run_race(
            build,
            lambda api, w: api.get_active_context(
                "pl", Role.PLAYER, {"player_id": w["player"]}),
            transfer)
        self._assert_between(c, (w["p1"], w["s1"]), (w["p2"], w["s2"]))

    # -- Guardian -----------------------------------------------------------
    def _guardian_world(self, api):
        pid, sid = _program_season(api, "P", "S")
        team = _team_registered(api, sid)
        junior = api.create_player(team, "Jun", "forward")["id"]
        ts = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with api.store.transaction():
            api.store.add_guardian_link(GuardianLink(
                id="gl_r", guardian_user_id="g", player_id=junior,
                created_at=ts, verified=True))
        return {"pid": pid, "sid": sid, "junior": junior}

    def test_guardian_revocation_vs_get_is_wholly_pre_or_post(self):
        def revoke(mapi, mstore, w):
            with mstore.transaction():
                link = mstore.guardian_links_for("g")[0]
                link.verified = False
                mstore.save_guardian_link(link)

        c, w = self._run_race(
            self._guardian_world,
            lambda api, w: api.get_active_context("g", Role.GUARDIAN, {}),
            revoke)
        self._assert_between(c, (w["pid"], w["sid"]), (None, None))

    def test_guardian_junior_transfer_vs_get_is_wholly_pre_or_post(self):
        def build(api):
            w = self._guardian_world(api)
            p2, s2 = _program_season(api, "P2", "S2")
            t2 = _team_registered(api, s2, "T2")
            w.update({"p2": p2, "s2": s2, "t2": t2})
            return w

        def transfer(mapi, mstore, w):
            with mstore.transaction():
                j = mstore.get_player(w["junior"])
                j.team_id = w["t2"]
                mstore.save_player(j)

        c, w = self._run_race(
            build,
            lambda api, w: api.get_active_context("g", Role.GUARDIAN, {}),
            transfer)
        self._assert_between(c, (w["pid"], w["sid"]), (w["p2"], w["s2"]))


class ContextAuthorizationRaceLocalTest(unittest.TestCase):
    """Memory/SQLite parity for the linearizable-authorization contract: the
    process-wide transaction lock fully serializes a concurrent revocation
    against an in-flight resolve, so the reader's result is wholly pre-change
    (never a hybrid) and the revocation applies strictly after."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_concurrent_revocation_is_serialized_not_hybrid(self):
        for label, store in self._stores():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid = _program_season(api, "P", "S")
                team = _team_registered(api, sid)
                oid, _ = _official_assigned(api, sid, team)
                off = (Role.OFFICIAL, {"official_id": oid})
                paused, release, restore = _pause_after_program_snapshot()
                out = {}

                def read():
                    out["c"] = api.get_active_context("o", *off)

                def revoke():
                    # BLOCKS on the process lock the paused reader still holds,
                    # so it can only apply AFTER the reader's resolve commits.
                    with store.transaction():
                        store.remove_official_assignment(
                            store.assignments_for_official(oid)[0].id)

                tr = threading.Thread(target=read)
                tv = threading.Thread(target=revoke)
                try:
                    tr.start()
                    self.assertTrue(paused.wait(20), label)
                    tv.start()          # contends for the lock, blocks
                    release.set()
                    tr.join(20)
                    tv.join(20)
                finally:
                    restore()
                c = out["c"]
                self.assertEqual((c["program_id"], c["season_id"]), (pid, sid),
                                 (label, c))                # wholly pre-change
                # The revocation serialized strictly after and has now applied.
                self.assertIsNone(
                    api.get_active_context("o", *off)["program_id"], label)
                _close(store)


class ActiveContextMigrationTest(unittest.TestCase):
    """Migration 044 applies to an ADOPTED (pre-044) database and a stored
    selection survives a real close/reopen — file-backed SQLite + PostgreSQL."""

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

    def _downgrade(self, store):
        """Simulate a genuinely pre-044 database.

        Every version that touches ``user_active_context`` is de-registered,
        not just 044: migration 049 (#345) and migration 051 (#159 review
        findings 2+5) both ALTER the very table 044 creates, so leaving either
        recorded while dropping 044 would simulate a state real forward-only
        migration can never produce (the table recreated by 044 without a
        later ALTER's column, yet that version marked applied and therefore
        skipped — exactly the "no column named generation" shape a real
        adopted database, which always runs 044 then 049 then 051 in order,
        can never reach). Forward-only ordering is what this test must
        exercise."""
        with store.transaction():
            cur = store.conn.cursor()
            cur.execute("DROP TABLE IF EXISTS user_active_context")
            for version in (_VERSION, _LEAGUE_VERSION, _GENERATION_VERSION):
                cur.execute(store.dialect.sql(
                    "DELETE FROM schema_migrations WHERE version = ?"),
                    (version,))

    def test_upgrade_from_043_and_reopen_preserves_selection(self):
        stamp = datetime(2027, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
        for label, loc in self._locations():
            store = SqlStore(loc)
            try:
                if store.backend == "postgres":
                    store.reset_schema()
                self._downgrade(store)
                self.assertNotIn(_VERSION, store.migration_status()["applied"],
                                 label)
                migrate(store.conn, store.dialect)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                store.set_active_context(ActiveContext(
                    id="user_x", program_id="prog_x", season_id="seas_x",
                    updated_at=stamp))
                store.close()
                store = SqlStore(loc)
                self.assertIn(_VERSION, store.migration_status()["applied"], label)
                rec = store.get_active_context("user_x")
                self.assertEqual((rec.program_id, rec.season_id, rec.updated_at),
                                 ("prog_x", "seas_x", stamp), label)
                # A Program-only (null Season) selection round-trips durably too.
                store.set_active_context(ActiveContext(
                    id="user_y", program_id="prog_y", season_id=None,
                    updated_at=stamp))
                store.close()
                store = SqlStore(loc)
                self.assertIsNone(store.get_active_context("user_y").season_id,
                                  label)
            finally:
                if store.backend == "postgres":
                    store.reset_schema()
                store.close()


class ActiveContextHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.pop("DATABASE_URL", None)   # the default in-memory demo store
        STATE.reset()                          # seeds demo Program/Season + accounts
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _req(self, method, path, body=None, opener=None, role=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        op = opener or urllib.request.build_opener()
        try:
            with op.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _login(self, username):
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        self._req("POST", "/api/auth/login",
                  {"username": username, "password": "demo"}, opener=op)
        return op

    def test_roundtrip_and_no_500_on_repeat(self):
        admin = self._login("admin")
        s, ctx = self._req("GET", "/api/context", opener=admin)
        self.assertEqual(s, 200, ctx)
        self.assertIsNotNone(ctx["program_id"], ctx)
        body = {"program_id": ctx["program_id"], "season_id": ctx["season_id"]}
        s1, a = self._req("POST", "/api/context", body, opener=admin)
        s2, b = self._req("POST", "/api/context", body, opener=admin)   # idempotent
        self.assertEqual((s1, s2), (200, 200), (a, b))
        self.assertEqual(a["season_id"], b["season_id"])

    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self._req("GET", "/api/context")[0], 401)
        self.assertEqual(self._req("POST", "/api/context",
                                   {"program_id": "p"})[0], 401)

    def test_identity_less_fallbacks_are_401_and_persist_nothing(self):
        # A per-user context needs a real SESSION. The identity-less demo
        # fallbacks — the X-Demo-Role header, and DEMO_HEADERLESS_ADMIN=1 — must
        # NOT enumerate (GET) or set (POST) a context: same stable 401, zero rows.
        store = STATE.api.store
        before = len(store.user_active_context)
        self.assertEqual(
            self._req("GET", "/api/context", role="league_admin")[0], 401)
        self.assertEqual(
            self._req("POST", "/api/context", {"program_id": "p", "season_id": None},
                      role="league_admin")[0], 401)
        os.environ["DEMO_HEADERLESS_ADMIN"] = "1"
        try:
            self.assertEqual(self._req("GET", "/api/context")[0], 401)
            self.assertEqual(
                self._req("POST", "/api/context", {"program_id": "p"})[0], 401)
        finally:
            os.environ.pop("DEMO_HEADERLESS_ADMIN", None)
        self.assertEqual(len(store.user_active_context), before)   # nothing written

    def test_concurrent_posts_are_all_200(self):
        # Concurrent POSTs for one authenticated user all return 200 (no 500 at
        # the HTTP boundary); the atomic upsert leaves one row.
        admin = self._login("admin")
        _, ctx = self._req("GET", "/api/context", opener=admin)
        body = {"program_id": ctx["program_id"], "season_id": ctx["season_id"]}
        results = {}
        barrier = threading.Barrier(2)

        def post(k):
            op = self._login("admin")
            barrier.wait()
            results[k] = self._req("POST", "/api/context", body, opener=op)[0]

        threads = [threading.Thread(target=post, args=(k,)) for k in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(set(results.values()), {200}, results)

    def test_strict_schema(self):
        admin = self._login("admin")
        # unknown field
        s, b = self._req("POST", "/api/context",
                         {"program_id": "p", "extra": 1}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "unknown_field")
        # missing required program_id
        s, b = self._req("POST", "/api/context", {"season_id": "s"}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "field_required")
        # wrong type
        s, b = self._req("POST", "/api/context", {"program_id": 5}, opener=admin)
        self.assertEqual(s, 400, b)
        self.assertEqual(b["error"]["details"]["reason"], "wrong_type")

    def test_options_session_only_and_scoped(self):
        # Identity-less callers are rejected exactly like /api/context.
        self.assertEqual(self._req("GET", "/api/context/options")[0], 401)
        self.assertEqual(
            self._req("GET", "/api/context/options", role="league_admin")[0], 401)
        # Admin sees options, and `selected` is one of the offered Programs.
        admin = self._login("admin")
        s, o = self._req("GET", "/api/context/options", opener=admin)
        self.assertEqual(s, 200, o)
        prog_ids = {p["id"] for p in o["programs"]}
        self.assertTrue(prog_ids, o)
        self.assertIn(o["selected"]["program_id"], prog_ids, o)
        # A scoped coach only enumerates its own Program through the boundary.
        coach = self._login("coach")
        s, oc = self._req("GET", "/api/context/options", opener=coach)
        self.assertEqual(s, 200, oc)
        self.assertEqual(len({p["id"] for p in oc["programs"]}), 1, oc)

    def test_scoped_coach_context_after_transfer(self):
        # End-to-end through the authenticated HTTP boundary: after the seeded
        # coach's Team transfers to a new League (its former Season archived and
        # frozen), the coach's /api/context falls back to the new active Season
        # and the prior Season is rejected as not_found — the same decided
        # behavior the facade matrix proves for Player/Guardian.
        STATE.reset()                                   # isolate: fresh demo seed
        try:
            api = STATE.api
            store = api.store
            program = STATE.ids["league_id"]            # the seed's Program id
            old_season = STATE.ids["season_id"]
            home = STATE.ids["home_team_id"]
            _archive(store, old_season)                 # freeze the seeded Season
            s2 = api.create_season(program, "Next")["id"]
            lb = api.create_league(s2, "Next Tier")["id"]
            db = api.create_division(s2, "Next Div", league_id=lb)["id"]
            moved = api.transfer_team_to_league(home, lb)
            self.assertNotIn("error", moved, moved)
            api.setup.register_team_for_season(s2, home, db, league_id=lb)
            coach = self._login("coach")
            s, ctx = self._req("GET", "/api/context", opener=coach)
            self.assertEqual(s, 200, ctx)
            self.assertEqual(ctx["program_id"], program, ctx)
            self.assertEqual(ctx["season_id"], s2, ctx)     # fallback → new active
            self.assertFalse(ctx["read_only"], ctx)
            # The prior (archived, former-league) Season is rejected through the
            # real session boundary as a non-oracle not_found.
            s, denied = self._req(
                "POST", "/api/context",
                {"program_id": program, "season_id": old_season}, opener=coach)
            self.assertEqual(s, 404, denied)
            self.assertEqual(denied["error"]["details"]["reason"],
                             "season_not_accessible", denied)
            # The current Season is selectable.
            s, ok = self._req("POST", "/api/context",
                              {"program_id": program, "season_id": s2},
                              opener=coach)
            self.assertEqual(s, 200, ok)
            self.assertEqual(ok["season_id"], s2, ok)
        finally:
            STATE.reset()                               # restore a clean seed

    def test_scoped_identity_through_the_real_boundary(self):
        # The seeded "coach" account is scoped to the demo home team. Its context
        # resolves to that team's Program, proving session role/scope threading —
        # and setting context still grants it no operator authority.
        coach = self._login("coach")
        s, ctx = self._req("GET", "/api/context", opener=coach)
        self.assertEqual(s, 200, ctx)
        self.assertIsNotNone(ctx["program_id"], ctx)
        s, _ = self._req("POST", "/api/context",
                         {"program_id": ctx["program_id"],
                          "season_id": ctx["season_id"]}, opener=coach)
        self.assertEqual(s, 200)
        # ...yet an operator write stays forbidden.
        s, denied = self._req("POST", "/api/setup/venue", {"name": "V"},
                              opener=coach)
        self.assertEqual(s, 403, denied)


if __name__ == "__main__":
    unittest.main()
