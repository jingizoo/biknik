"""get_setup_overview_v2's authorized-Program-set scoping axis (#367).

``test_v2_setup_contract.py`` already covers this endpoint's canonical field
shapes over real HTTP (including the additive ``teams[].league_id`` DTO field
itself, in ``test_v2_overview_canonical_flat_lists``) — this file does not
repeat that. It covers only what #367 actually changed: the endpoint used to
be fully global/unfiltered (every Program in the installation, regardless of
caller); it now scopes Programs/Seasons/Leagues/Divisions/Teams to the
caller's full ``context_scope.authorized_program_ids`` set when a real
``role`` is supplied, while leaving the no-role default (still used by a
couple of pre-#367 direct call sites, e.g. ``test_season_lifecycle.py``) and
the Venue/Rink/IceSlot/Club/Organization/Official reference data fully
unfiltered either way.

Facade-level only (``ApiService(InMemoryStore())`` directly): the HTTP route's
auth gate and canonical shape are already exercised end to end in
``test_v2_setup_contract.py``; the scoping logic itself has no HTTP-specific
angle, so one facade-level pass is sufficient here, mirroring
``test_setup_progress.py``'s own facade-vs-HTTP split.

Today only League Admin and Arena Manager reach this endpoint via HTTP, and
both are "global" roles (``context_scope._GLOBAL_ROLES``) — so scoping is a
real no-op for them. To actually exercise the narrowing, some tests below
call the facade directly with a Coach role/scope (whose ``authorized_
program_ids`` narrows to a single Team's Program, per ``context_scope.py``)
even though no HTTP route currently lets a Coach reach this endpoint — the
facade-level authorization logic must still narrow correctly for ANY
role/scope it is given, independent of which roles the route permits today.
"""

import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore

_ALL_OVERVIEW_KEYS = (
    "programs", "seasons", "leagues", "divisions", "teams", "clubs",
    "organizations", "officials", "venues", "rinks", "ice_slots",
)


class LeagueFilteredOverviewV2Test(unittest.TestCase):
    """Facade-level, Memory-backed: mirrors ``SetupProgressComputationTest``'s
    own reasoning (a pure read composed from store methods that already carry
    their own Memory/SQLite/PostgreSQL parity coverage elsewhere)."""

    def _api(self):
        return ApiService(InMemoryStore())

    def _build_stack(self, api, label):
        """One full Program → Season → League → Division → Club → Team stack
        labeled ``label``, with the Team's PERMANENT competition League
        (``Team.league_id``, #283) set explicitly."""
        program = api.create_program(f"Program {label}")
        self.assertNotIn("error", program, program)
        season = api.create_season(program["id"], f"Season {label}")
        self.assertNotIn("error", season, season)
        league = api.create_league(season["id"], f"League {label}")
        self.assertNotIn("error", league, league)
        division = api.create_division(season["id"], f"Division {label}",
                                       league_id=league["id"])
        self.assertNotIn("error", division, division)
        club = api.create_club(f"Club {label}")
        self.assertNotIn("error", club, club)
        team = api.create_team(club_id=club["id"], division_id=division["id"],
                               name=f"Team {label}", program_id=program["id"],
                               league_id=league["id"])
        self.assertNotIn("error", team, team)
        return {"program": program, "season": season, "league": league,
                "division": division, "club": club, "team": team}

    def _ids(self, ov, key):
        return {row["id"] for row in ov[key]}

    # -- 1. no-role default stays the full, unfiltered installation view -----
    def test_no_role_returns_full_unfiltered_installation_view(self):
        """Unchanged pre-#367 behavior when ``role`` is left ``None`` (the
        default) — confirming the exact default a couple of pre-#367 direct
        call sites (``test_season_lifecycle.py``) rely on, unmodified."""
        api = self._api()
        a = self._build_stack(api, "A")
        b = self._build_stack(api, "B")

        ov = api.get_setup_overview_v2()
        self.assertNotIn("error", ov, ov)

        self.assertEqual(
            {a["program"]["id"], b["program"]["id"]} - self._ids(ov, "programs"),
            set())
        self.assertEqual(
            {a["season"]["id"], b["season"]["id"]} - self._ids(ov, "seasons"),
            set())
        self.assertEqual(
            {a["league"]["id"], b["league"]["id"]} - self._ids(ov, "leagues"),
            set())
        self.assertEqual(
            {a["division"]["id"], b["division"]["id"]}
            - self._ids(ov, "divisions"), set())
        self.assertEqual(
            {a["team"]["id"], b["team"]["id"]} - self._ids(ov, "teams"), set())

    # -- 2. global role (today's actual callers) sees EVERY Program ----------
    def test_global_role_sees_both_programs_full_structure(self):
        """League Admin (a ``_GLOBAL_ROLES`` member) supplies a real role/scope
        but must see NO behavior change from point 1 — the "no visible
        behavior change for today's actual roles" half of the #367 design."""
        api = self._api()
        a = self._build_stack(api, "A")
        b = self._build_stack(api, "B")

        ov = api.get_setup_overview_v2("admin1", Role.LEAGUE_ADMIN, {})
        self.assertNotIn("error", ov, ov)

        for key, stack_key in (("programs", "program"), ("seasons", "season"),
                               ("leagues", "league"),
                               ("divisions", "division"),
                               ("teams", "team")):
            ids = self._ids(ov, key)
            self.assertIn(a[stack_key]["id"], ids, (key, "A missing"))
            self.assertIn(b[stack_key]["id"], ids, (key, "B missing"))

    # -- 3. a scoped role/scope actually narrows ------------------------------
    def test_scoped_role_narrows_to_single_authorized_program(self):
        """A Coach scoped to a Team in Program A (``context_scope.
        authorized_program_ids`` resolves a Coach to exactly its own Team's
        Program) sees ONLY Program A's structure — Program B's
        programs/seasons/leagues/divisions/teams are entirely absent. No HTTP
        route lets a Coach reach this endpoint today; the facade's own
        authorization logic is what's under test here."""
        api = self._api()
        a = self._build_stack(api, "A")
        b = self._build_stack(api, "B")

        scope = {"team_id": a["team"]["id"]}
        ov = api.get_setup_overview_v2("coach1", Role.COACH, scope)
        self.assertNotIn("error", ov, ov)

        for key, stack_key in (("programs", "program"), ("seasons", "season"),
                               ("leagues", "league"),
                               ("divisions", "division"),
                               ("teams", "team")):
            ids = self._ids(ov, key)
            self.assertIn(a[stack_key]["id"], ids, (key, "A should be visible"))
            self.assertNotIn(b[stack_key]["id"], ids,
                             (key, "B should be narrowed out"))

    # -- 4. an empty authorized set is a fully-empty shape, not an error -----
    def test_empty_authorized_program_set_returns_fully_empty_shape(self):
        """A Coach whose ``team_id`` resolves to no Team at all (context_scope
        fails CLOSED — see ``authorized_program_ids``: ``own_team_id`` returns
        the dangling id, ``store.get_team`` returns ``None``, so the pid is
        ``None`` and the authorized set is empty) gets every key back as an
        empty list — never an error — even though real data exists in the
        installation."""
        api = self._api()
        self._build_stack(api, "A")  # real data exists; must still be hidden

        scope = {"team_id": "team_does_not_exist"}
        ov = api.get_setup_overview_v2("coach2", Role.COACH, scope)
        self.assertNotIn("error", ov, ov)

        for key in _ALL_OVERVIEW_KEYS:
            self.assertEqual(ov[key], [], key)

    # -- 5. Venues/Rinks/IceSlots/Clubs/Organizations/Officials stay global --
    def test_reference_data_stays_unfiltered_despite_program_narrowing(self):
        """Even while Programs/Seasons/Leagues/Divisions/Teams are narrowed to
        one authorized Program (per point 3), Venue/Rink/IceSlot/Club/
        Organization/Official data belonging to (or associated with) a
        DIFFERENT, unauthorized Program still appears in full — a deliberate,
        disclosed design choice (Venues are Organization-owned, not
        Program-owned; Clubs/Organizations/Officials have no Program link in
        the domain model at all), not a scoping gap. This must never get
        "fixed" by a future refactor: the Setup Records creation workflow
        needs to see all reference data when creating new structure."""
        api = self._api()
        a = self._build_stack(api, "A")
        b = self._build_stack(api, "B")

        org_b = api.create_organization("Org B")
        self.assertNotIn("error", org_b, org_b)
        venue_b = api.create_venue("Venue B", organization_id=org_b["id"])
        self.assertNotIn("error", venue_b, venue_b)
        rink_b = api.create_rink(venue_b["id"], "Rink B")
        self.assertNotIn("error", rink_b, rink_b)
        slot_b = api.create_ice_slot(rink_b["id"], "2026-09-01T18:30:00+00:00",
                                     "2026-09-01T20:00:00+00:00")
        self.assertNotIn("error", slot_b, slot_b)
        official_b = api.create_official("Ref B", home_club_id=b["club"]["id"])
        self.assertNotIn("error", official_b, official_b)

        scope = {"team_id": a["team"]["id"]}
        ov = api.get_setup_overview_v2("coach3", Role.COACH, scope)
        self.assertNotIn("error", ov, ov)

        # Narrowed axis: Program B's own competition structure is absent.
        self.assertNotIn(b["program"]["id"], self._ids(ov, "programs"))
        self.assertNotIn(b["team"]["id"], self._ids(ov, "teams"))

        # Unfiltered axis: every piece of Program-B-associated reference data
        # still shows up whole.
        self.assertIn(b["club"]["id"], self._ids(ov, "clubs"))
        self.assertIn(org_b["id"], self._ids(ov, "organizations"))
        self.assertIn(venue_b["id"], self._ids(ov, "venues"))
        self.assertIn(rink_b["id"], self._ids(ov, "rinks"))
        self.assertIn(slot_b["id"], self._ids(ov, "ice_slots"))
        self.assertIn(official_b["id"], self._ids(ov, "officials"))

    # -- 6. teams[].league_id (real competition League) survives narrowing --
    def test_team_league_id_field_correct_after_program_narrowing(self):
        """Within a narrowed (single-authorized-Program) view, a Team's
        ``league_id`` is still the correct real competition-League id
        (``Team.league_id``, #283) — not dropped or nulled by the Program-
        scoping logic. (The DTO shape itself, including this field's
        presence, is already covered by
        ``test_v2_overview_canonical_flat_lists`` in
        test_v2_setup_contract.py; this is only the scoping interaction.)"""
        api = self._api()
        a = self._build_stack(api, "A")

        scope = {"team_id": a["team"]["id"]}
        ov = api.get_setup_overview_v2("coach4", Role.COACH, scope)
        self.assertNotIn("error", ov, ov)

        row = next(t for t in ov["teams"] if t["id"] == a["team"]["id"])
        self.assertEqual(row["league_id"], a["league"]["id"])
        self.assertEqual(row["program_id"], a["program"]["id"])


if __name__ == "__main__":
    unittest.main()
