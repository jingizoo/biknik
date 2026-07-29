"""``get_setup_overview_v2``'s active-Program scoping axis (#369 rearchitecture).

This file replaces the pre-review version of itself, which encoded the OLD
(#367-era) contract: a scoped role saw its FULL authorized Program set, and
Club/Organization/Official/Venue/Rink/IceSlot reference data stayed globally
unfiltered no matter what. #369 review corrected both of those:

* ``programs`` (and everything derived from it) now collapses to the single
  persisted ACTIVE Program (``ContextService.resolve_with_league`` — the same
  ceiling the Dashboard and Setup Progress reads already use), never the
  caller's whole authorized set. The context BAR is the cross-Program picker;
  this structural surface only ever operates within whichever one Program is
  currently active. A selected League further narrows Teams/Divisions only —
  Season is NOT a further filter here (unlike the Dashboard): every Season of
  the active Program is always offered.
* Club/Organization/Official/Venue/Rink/IceSlot are no longer disclosed
  globally: each is included only via a real, validatable chain into the
  active Program (a Club with >=1 Team there, an Organization owning an
  in-scope Venue, a Venue with an active ``SeasonVenueAccess`` grant to one of
  the Program's Seasons, an Official whose home Club is in-scope or who has an
  assignment there). The additive ``unassigned_*`` lists carry records with NO
  chain to ANY Program at all, so create-then-link UI flows (assign a fresh
  Club to a Team, grant a fresh Venue to a Season, ...) keep working without
  reintroducing cross-Program leakage.
* A brand-new install with ZERO Programs anywhere still gets the full
  unfiltered legacy shape (nothing exists yet to leak); a role authorized for
  ZERO Programs while OTHER Programs already exist instead gets the fully
  scoped-empty shape (every derived-join set is naturally empty too, since
  nothing can validate against a Program that never resolved) except for the
  ``unassigned_*`` lists, which stay populated — they disclose nothing
  Program-specific by construction.

Facade-level only (``ApiService`` directly over each supported store): the
HTTP route's auth gate and canonical DTO shape are already exercised end to
end in ``test_v2_setup_contract.py``; this file is the scoping logic itself,
across the full Memory/SQLite/(optional)PostgreSQL backend matrix per the
review's explicit "parametrize the store" requirement (mirroring
``test_active_context_league.py``), and across the full role matrix (League
Admin, Coach, Player, Guardian, Official, Arena Manager, Viewer) per the
review's explicit "exercise every role, not just Coach/League Admin"
requirement.
"""

import os
import unittest
from datetime import datetime, timezone

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import GuardianLink, OfficialRole, Role
from hockey_scheduler.domain.models import Game
from hockey_scheduler.domain.setup_models import OfficialAssignment
from hockey_scheduler.store import InMemoryStore, SqlStore

_ALL_OVERVIEW_KEYS = (
    "programs", "seasons", "leagues", "divisions", "teams", "clubs",
    "organizations", "officials", "venues", "rinks", "ice_slots",
)
_ALL_UNASSIGNED_KEYS = (
    "unassigned_clubs", "unassigned_organizations", "unassigned_officials",
    "unassigned_venues", "unassigned_rinks", "unassigned_ice_slots",
)

_TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

# Every role the endpoint must scope correctly, independent of which roles the
# HTTP route permits today (#369 review: the facade's own authorization logic
# must narrow correctly for ANY role/scope it is given).
_ROLE_MATRIX = (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER,
               Role.COACH, Role.PLAYER, Role.GUARDIAN, Role.OFFICIAL)


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        # fresh_sql_store (not a bare SqlStore) so a database polluted by an
        # earlier module in this same serial worker is recovered rather than
        # failing to open at all -- see helpers.fresh_sql_store (#369).
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _assign_game(api, official_id, *, season_id, team_id):
    """Assign ``official_id`` to a minimal REGULAR Game under ``season_id`` —
    enough for ``context_scope``'s Official-authorization branch (which reads
    only the Game's ``season_id``) and for the endpoint's own
    assignment-based Official-inclusion branch (same field)."""
    store = api.store
    with store.transaction():
        gid = store.next_id("game")
        store.add_game(Game(id=gid, home_team_id=team_id, away_team_id=team_id,
                            start_time=None, season_id=season_id))
        store.add_official_assignment(OfficialAssignment(
            id=store.next_id("assign"), game_id=gid, official_id=official_id,
            role=OfficialRole.REFEREE))
    return gid


class LeagueFilteredOverviewV2Test(unittest.TestCase):
    """Facade-level, Memory/SQLite/(optional)PostgreSQL-parametrized: mirrors
    ``test_active_context_league.py``'s own backend-matrix discipline for a
    pure read composed from store methods."""

    def _ids(self, ov, key):
        return {row["id"] for row in ov[key]}

    def _build_program_env(self, api, label):
        """One two-League Program environment labeled ``label``: 2 Seasons,
        2 sibling Leagues (each bound to its own Season), 2 Divisions, 2
        Clubs, 2 Teams (one permanently in each League, registered into its
        League's Season), 1 Organization owning 1 Venue (granted active
        ``SeasonVenueAccess`` to Season 1), 1 Rink, 1 IceSlot, and 1 Official
        (home club = Club 1) — rich enough to prove both the Program axis
        (vs. a sibling Program) and the League axis (vs. a sibling League
        within the SAME Program)."""
        program = api.create_program(f"Program {label}", "US", "UTC")
        self.assertNotIn("error", program, program)
        season1 = api.create_season(program["id"], f"Season {label}1")
        self.assertNotIn("error", season1, season1)
        season2 = api.create_season(program["id"], f"Season {label}2")
        self.assertNotIn("error", season2, season2)
        league1 = api.create_league(season1["id"], f"League {label}1")
        self.assertNotIn("error", league1, league1)
        league2 = api.create_league(season2["id"], f"League {label}2")
        self.assertNotIn("error", league2, league2)
        division1 = api.create_division(season1["id"], f"Division {label}1",
                                        league_id=league1["id"])
        self.assertNotIn("error", division1, division1)
        division2 = api.create_division(season2["id"], f"Division {label}2",
                                        league_id=league2["id"])
        self.assertNotIn("error", division2, division2)
        club1 = api.create_club(f"Club {label}1")
        self.assertNotIn("error", club1, club1)
        club2 = api.create_club(f"Club {label}2")
        self.assertNotIn("error", club2, club2)
        team1 = api.create_team(club_id=club1["id"], division_id=division1["id"],
                                name=f"Team {label}1", program_id=program["id"],
                                league_id=league1["id"])
        self.assertNotIn("error", team1, team1)
        team2 = api.create_team(club_id=club2["id"], division_id=division2["id"],
                                name=f"Team {label}2", program_id=program["id"],
                                league_id=league2["id"])
        self.assertNotIn("error", team2, team2)
        reg1 = api.register_team_for_season(season1["id"], team1["id"],
                                            division1["id"],
                                            league_id=league1["id"])
        self.assertNotIn("error", reg1, reg1)
        reg2 = api.register_team_for_season(season2["id"], team2["id"],
                                            division2["id"],
                                            league_id=league2["id"])
        self.assertNotIn("error", reg2, reg2)
        org = api.create_organization(f"Org {label}")
        self.assertNotIn("error", org, org)
        venue = api.create_venue(f"Venue {label}", organization_id=org["id"])
        self.assertNotIn("error", venue, venue)
        sva = api.grant_season_venue_access(season1["id"], venue["id"])
        self.assertNotIn("error", sva, sva)
        rink = api.create_rink(venue["id"], f"Rink {label}")
        self.assertNotIn("error", rink, rink)
        slot = api.create_ice_slot(
            rink["id"], "2026-09-01T18:30:00+00:00", "2026-09-01T20:00:00+00:00")
        self.assertNotIn("error", slot, slot)
        official = api.create_official(f"Official {label}",
                                       home_club_id=club1["id"])
        self.assertNotIn("error", official, official)
        return {"program": program, "season1": season1, "season2": season2,
                "league1": league1, "league2": league2,
                "division1": division1, "division2": division2,
                "club1": club1, "club2": club2, "team1": team1, "team2": team2,
                "org": org, "venue": venue, "rink": rink, "slot": slot,
                "official": official}

    def _role_scope_for(self, api, role, env, label):
        """``(user_id, scope, extra_official_id)`` for ``role``, authorized
        for ONLY ``env``'s Program — constructing whatever real subject row
        (Player / assigned Official / verified GuardianLink) that role's own
        ``context_scope`` resolution requires. ``extra_official_id`` is the id
        of a side-effect Official row (Official role only); callers must
        account for its presence in ``officials``."""
        team1 = env["team1"]
        if role in (Role.LEAGUE_ADMIN, Role.ARENA_MANAGER, Role.VIEWER):
            return f"user_{role.value}_{label}", {}, None
        if role == Role.COACH:
            return f"coach_{label}", {"team_id": team1["id"]}, None
        if role == Role.PLAYER:
            player = api.create_player(team1["id"], f"Player {label}", "forward")
            self.assertNotIn("error", player, player)
            return f"player_{label}", {"player_id": player["id"]}, None
        if role == Role.OFFICIAL:
            official = api.create_official(f"Caller Official {label}")
            self.assertNotIn("error", official, official)
            _assign_game(api, official["id"], season_id=env["season1"]["id"],
                        team_id=team1["id"])
            return (f"official_{label}", {"official_id": official["id"]},
                    official["id"])
        if role == Role.GUARDIAN:
            player = api.create_player(team1["id"], f"Junior {label}", "forward")
            self.assertNotIn("error", player, player)
            uid = f"guardian_{label}"
            with api.store.transaction():
                api.store.add_guardian_link(GuardianLink(
                    id=api.store.next_id("glink"), guardian_user_id=uid,
                    player_id=player["id"], created_at=_TS, verified=True))
            return uid, {}, None
        raise AssertionError(f"unhandled role {role}")

    # -- 1. no-role default stays the full, unfiltered installation view -----
    def test_no_role_returns_full_unfiltered_installation_view(self):
        """Unchanged legacy behavior when ``role`` is left ``None`` (the
        default) — a couple of pre-#367 direct call sites
        (``test_season_lifecycle.py``) rely on this exact shape."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                b = self._build_program_env(api, "B")

                ov = api.get_setup_overview_v2()
                self.assertNotIn("error", ov, ov)

                expected = (
                    ("programs", {a["program"]["id"], b["program"]["id"]}),
                    ("seasons", {a["season1"]["id"], a["season2"]["id"],
                                b["season1"]["id"], b["season2"]["id"]}),
                    ("leagues", {a["league1"]["id"], a["league2"]["id"],
                                b["league1"]["id"], b["league2"]["id"]}),
                    ("divisions", {a["division1"]["id"], a["division2"]["id"],
                                  b["division1"]["id"], b["division2"]["id"]}),
                    ("teams", {a["team1"]["id"], a["team2"]["id"],
                              b["team1"]["id"], b["team2"]["id"]}),
                    ("clubs", {a["club1"]["id"], a["club2"]["id"],
                              b["club1"]["id"], b["club2"]["id"]}),
                    ("organizations", {a["org"]["id"], b["org"]["id"]}),
                    ("venues", {a["venue"]["id"], b["venue"]["id"]}),
                    ("rinks", {a["rink"]["id"], b["rink"]["id"]}),
                    ("ice_slots", {a["slot"]["id"], b["slot"]["id"]}),
                    ("officials", {a["official"]["id"], b["official"]["id"]}),
                )
                for key, ids in expected:
                    self.assertEqual(self._ids(ov, key), ids, (label, key))

                # unassigned_* are always empty in the unfiltered shape — the
                # main lists above are already unfiltered, so there is nothing
                # additive left to offer.
                for key in _ALL_UNASSIGNED_KEYS:
                    self.assertEqual(ov[key], [], (label, key))
                _close(store)

    # -- 2. zero Programs anywhere: bootstrap matches the no-role shape ------
    def test_bootstrap_with_zero_programs_matches_the_unfiltered_norole_shape(self):
        """A brand-new install with LITERALLY no Program yet: even a real
        role/scope must fall through to the exact same unfiltered shape as
        ``role=None`` — there is no "other Program" for such an install to
        leak, so this is a bootstrap read, not a denial."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                no_role = api.get_setup_overview_v2()
                self.assertNotIn("error", no_role, no_role)
                with_role = api.get_setup_overview_v2(
                    "someone", Role.COACH, {"team_id": "team_does_not_exist"})
                self.assertNotIn("error", with_role, with_role)

                self.assertEqual(with_role, no_role, label)
                for key in _ALL_OVERVIEW_KEYS + _ALL_UNASSIGNED_KEYS:
                    self.assertEqual(no_role[key], [], (label, key))
                _close(store)

    # -- 3. every role in the matrix narrows to its OWN active Program -------
    def test_role_matrix_each_role_narrows_to_its_active_program(self):
        """League Admin, Arena Manager, Viewer, Coach, Player, Guardian, and
        Official each independently resolve to Program A (never Program B,
        which also exists in the same installation) once Program A is set as
        the active context — proving both the full role matrix (#369 review)
        and that EVERY collection (Programs/Seasons/Leagues/Divisions/Teams/
        Clubs/Organizations/Officials/Venues/Rinks/IceSlots) excludes a
        sibling Program's rows, for every role, not just Coach/League Admin."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                self._build_program_env(api, "B")  # never expected to appear

                for role in _ROLE_MATRIX:
                    with self.subTest(backend=label, role=role.value):
                        user_id, scope, extra_official_id = (
                            self._role_scope_for(api, role, a, "A"))
                        ctx = api.set_active_context(
                            user_id, role, scope, a["program"]["id"], None, None)
                        self.assertNotIn("error", ctx, (label, role, ctx))
                        self.assertEqual(ctx["program_id"], a["program"]["id"])

                        ov = api.get_setup_overview_v2(user_id, role, scope)
                        self.assertNotIn("error", ov, (label, role, ov))

                        expected = (
                            ("programs", {a["program"]["id"]}),
                            ("seasons", {a["season1"]["id"],
                                        a["season2"]["id"]}),
                            ("leagues", {a["league1"]["id"],
                                        a["league2"]["id"]}),
                            ("divisions", {a["division1"]["id"],
                                          a["division2"]["id"]}),
                            ("teams", {a["team1"]["id"], a["team2"]["id"]}),
                            ("clubs", {a["club1"]["id"], a["club2"]["id"]}),
                            ("organizations", {a["org"]["id"]}),
                            ("venues", {a["venue"]["id"]}),
                            ("rinks", {a["rink"]["id"]}),
                            ("ice_slots", {a["slot"]["id"]}),
                        )
                        for key, ids in expected:
                            # Exact equality: since it never includes any of
                            # Program B's (differently-id'd) rows, this proves
                            # exclusion, not merely presence of Program A's.
                            self.assertEqual(
                                self._ids(ov, key), ids, (label, role, key))

                        expected_officials = {a["official"]["id"]}
                        if extra_official_id:
                            expected_officials.add(extra_official_id)
                        self.assertEqual(
                            self._ids(ov, "officials"), expected_officials,
                            (label, role))
                _close(store)

    # -- 4. a selected League narrows Teams/Divisions only -------------------
    def test_selected_league_narrows_teams_and_divisions_but_not_program_level_data(self):
        """Within Program A (which has two sibling Leagues, A1 and A2),
        selecting League A1 narrows Teams/Divisions down to League A1's own —
        but Clubs/Organizations/Venues/Rinks/IceSlots/Officials are Program-
        level, not League-level, and stay at their full Program-A scope
        (both Leagues' Clubs still show). Seasons/Leagues themselves are
        never narrowed by the selection either — every one of the Program's
        is still offered so the operator can pick a different one."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")

                scope = {"team_id": a["team1"]["id"]}
                ctx = api.set_active_context(
                    "coach_a", Role.COACH, scope, a["program"]["id"], None,
                    a["league1"]["id"])
                self.assertNotIn("error", ctx, ctx)
                self.assertEqual(ctx["league_id"], a["league1"]["id"], ctx)

                ov = api.get_setup_overview_v2("coach_a", Role.COACH, scope)
                self.assertNotIn("error", ov, ov)

                # League-narrowed axis: only League A1's own Team/Division.
                self.assertEqual(self._ids(ov, "teams"), {a["team1"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "divisions"),
                                 {a["division1"]["id"]}, label)

                # Program-level axis: untouched by the League selection.
                self.assertEqual(
                    self._ids(ov, "clubs"),
                    {a["club1"]["id"], a["club2"]["id"]}, label)
                self.assertEqual(self._ids(ov, "organizations"),
                                 {a["org"]["id"]}, label)
                self.assertEqual(self._ids(ov, "venues"), {a["venue"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "rinks"), {a["rink"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "ice_slots"), {a["slot"]["id"]},
                                 label)
                self.assertEqual(self._ids(ov, "officials"),
                                 {a["official"]["id"]}, label)

                # Never narrowed by League OR Season: every one of the
                # Program's Seasons/Leagues stays offered.
                self.assertEqual(
                    self._ids(ov, "seasons"),
                    {a["season1"]["id"], a["season2"]["id"]}, label)
                self.assertEqual(
                    self._ids(ov, "leagues"),
                    {a["league1"]["id"], a["league2"]["id"]}, label)

                # The additive Team.league_id DTO field (#283) survives.
                row = next(t for t in ov["teams"] if t["id"] == a["team1"]["id"])
                self.assertEqual(row["league_id"], a["league1"]["id"], label)
                self.assertEqual(row["program_id"], a["program"]["id"], label)
                _close(store)

    # -- 5. unassigned_* buckets: genuinely-unlinked records, from EITHER side
    def test_unassigned_buckets_show_genuinely_unlinked_records_from_either_program(self):
        """A Club/Organization/Venue/Rink/IceSlot/Official linked to NO
        Program at all shows up in the RIGHT ``unassigned_*`` bucket
        regardless of which Program is currently active — Program A and
        Program B each see the exact same never-linked record in their own
        ``unassigned_*`` list, since it discloses nothing Program-specific.
        A record linked to ONE specific Program, by contrast, never appears in
        ``unassigned_*`` from either Program's perspective — it belongs to
        someone else, not to nobody."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                a = self._build_program_env(api, "A")
                b = self._build_program_env(api, "B")

                free_club = api.create_club("Free Club")
                self.assertNotIn("error", free_club, free_club)
                free_org = api.create_organization("Free Org")
                self.assertNotIn("error", free_org, free_org)
                # Deliberately no organization_id: an Organization is only
                # "unassigned" when it owns ZERO Venues anywhere (see
                # get_setup_overview_v2's org_ids_any) -- owning even a
                # grant-less Venue would disqualify it. A Venue owned by NO
                # Organization at all keeps the two unassigned checks
                # (Organization / Venue) fully independent of each other.
                free_venue = api.create_venue("Free Venue")
                self.assertNotIn("error", free_venue, free_venue)
                free_rink = api.create_rink(free_venue["id"], "Free Rink")
                self.assertNotIn("error", free_rink, free_rink)
                free_slot = api.create_ice_slot(
                    free_rink["id"], "2026-10-01T10:00:00+00:00",
                    "2026-10-01T11:00:00+00:00")
                self.assertNotIn("error", free_slot, free_slot)
                free_official = api.create_official("Free Official")
                self.assertNotIn("error", free_official, free_official)

                for chosen_label, env, other in (("A", a, b), ("B", b, a)):
                    admin_id = f"admin_{chosen_label}"
                    ctx = api.set_active_context(
                        admin_id, Role.LEAGUE_ADMIN, {}, env["program"]["id"],
                        None, None)
                    self.assertNotIn("error", ctx, ctx)
                    ov = api.get_setup_overview_v2(admin_id, Role.LEAGUE_ADMIN,
                                                   {})
                    self.assertNotIn("error", ov, ov)

                    # The genuinely-free records show up as unassigned no
                    # matter which Program is active.
                    self.assertIn(free_club["id"],
                                 self._ids(ov, "unassigned_clubs"),
                                 (label, chosen_label))
                    self.assertIn(free_org["id"],
                                 self._ids(ov, "unassigned_organizations"),
                                 (label, chosen_label))
                    self.assertIn(free_venue["id"],
                                 self._ids(ov, "unassigned_venues"),
                                 (label, chosen_label))
                    self.assertIn(free_rink["id"],
                                 self._ids(ov, "unassigned_rinks"),
                                 (label, chosen_label))
                    self.assertIn(free_slot["id"],
                                 self._ids(ov, "unassigned_ice_slots"),
                                 (label, chosen_label))
                    self.assertIn(free_official["id"],
                                 self._ids(ov, "unassigned_officials"),
                                 (label, chosen_label))

                    # A record already linked to ONE specific Program (own OR
                    # the other) never leaks into unassigned_* either way.
                    for owner in (env, other):
                        self.assertNotIn(
                            owner["club1"]["id"],
                            self._ids(ov, "unassigned_clubs"),
                            (label, chosen_label))
                        self.assertNotIn(
                            owner["org"]["id"],
                            self._ids(ov, "unassigned_organizations"),
                            (label, chosen_label))
                        self.assertNotIn(
                            owner["venue"]["id"],
                            self._ids(ov, "unassigned_venues"),
                            (label, chosen_label))
                        self.assertNotIn(
                            owner["rink"]["id"],
                            self._ids(ov, "unassigned_rinks"),
                            (label, chosen_label))
                        self.assertNotIn(
                            owner["slot"]["id"],
                            self._ids(ov, "unassigned_ice_slots"),
                            (label, chosen_label))
                        self.assertNotIn(
                            owner["official"]["id"],
                            self._ids(ov, "unassigned_officials"),
                            (label, chosen_label))
                _close(store)

    # -- 6. zero-authorized-program: fully empty, except unassigned_* --------
    def test_scoped_role_with_no_resolved_program_is_fully_empty_but_unassigned_still_shows(self):
        """A Coach whose ``team_id`` resolves to no Team at all (context_scope
        fails CLOSED: ``own_team_id`` returns the dangling id, ``get_team``
        returns ``None``, so the authorized-program set is empty) in a store
        that DOES have other real Programs: every key that depends on a
        resolved Program comes back empty — never an error — including
        Clubs/Organizations/Officials/Venues/Rinks/IceSlots (#369 review
        correction: these are no longer globally unfiltered, so an
        unauthorized-for-every-Program caller gets none of them either). The
        additive ``unassigned_*`` buckets are unaffected by the failed
        resolution — they hold records linked to NO Program at all, which
        disclose nothing Program-specific regardless of who is asking."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self._build_program_env(api, "A")  # real data exists elsewhere

                free_club = api.create_club("Free Club")
                self.assertNotIn("error", free_club, free_club)
                free_official = api.create_official("Free Official")
                self.assertNotIn("error", free_official, free_official)

                scope = {"team_id": "team_does_not_exist"}
                ov = api.get_setup_overview_v2("coach_dangling", Role.COACH,
                                               scope)
                self.assertNotIn("error", ov, ov)

                for key in _ALL_OVERVIEW_KEYS:
                    self.assertEqual(ov[key], [], (label, key))

                self.assertIn(free_club["id"],
                             self._ids(ov, "unassigned_clubs"), label)
                self.assertIn(free_official["id"],
                             self._ids(ov, "unassigned_officials"), label)
                _close(store)

    # -- 7. Viewer: a pure read, no mutation ---------------------------------
    def test_viewer_role_is_a_pure_read(self):
        """A sanity check that calling this facade method with ``Role.VIEWER``
        never itself mutates store state — it is a pure read composed from
        store query methods. (Viewer's actual permission gate — rejecting
        mutation ENDPOINTS — is enforced at the HTTP/web authorization layer,
        not this Python facade; a facade-level test has no HTTP request to
        fabricate, so that half of the Viewer contract is out of scope here by
        design.)"""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                self._build_program_env(api, "A")

                def _snapshot():
                    return {
                        "programs": {p.id for p in api.store.all_programs()},
                        "seasons": {s.id for s in api.store.all_seasons()},
                        "leagues": {lg.id for lg in api.store.all_leagues()},
                        "divisions": {d.id for d in api.store.all_divisions()},
                        "teams": {t.id for t in api.store.all_teams()},
                        "clubs": {c.id for c in api.store.all_clubs()},
                        "organizations":
                            {o.id for o in api.store.all_organizations()},
                        "officials": {o.id for o in api.store.all_officials()},
                        "venues": {v.id for v in api.store.all_venues()},
                        "rinks": {r.id for r in api.store.all_rinks()},
                        "ice_slots": {s.id for s in api.store.all_ice_slots()},
                    }

                before = _snapshot()
                ov = api.get_setup_overview_v2("viewer_1", Role.VIEWER, {})
                self.assertNotIn("error", ov, ov)
                after = _snapshot()
                self.assertEqual(before, after, label)
                _close(store)


if __name__ == "__main__":
    unittest.main()
