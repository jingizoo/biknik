"""The facility-tree exception to the active-Program ceiling (#369 ruling).

`get_setup_overview_v2` ceilings every list to the active Program. That made
cross-Program venue sharing impossible -- and not merely awkward, but
DEADLOCKED on its own first use: once Program A grants itself access to an
arena, the Venue leaves Program B's scoped `venues`, and it is not
`pending_link_venues` either (it IS linked), so B can never grant itself the
access that would have made it visible. `venue-sharing.js` step 4 requires
exactly that grant, because an arena serves several leagues.

The ruling: the ceiling governs the COMPETITION tree (Seasons, Leagues,
Divisions, Teams, and the personal names on Players and Officials). The
facility tree is joined to it only by `SeasonVenueAccess`, so a Venue stays
OFFERABLE across Programs for the single purpose of granting access.

This module pins the exception's WIDTH, which is the whole risk in it. An
exception that quietly grew to Rinks, IceSlots, or anything in the competition
tree would be a general cross-Program leak wearing a feature's name, and every
assertion here exists to fail if that happens:

* `grantable_venues` DOES carry a foreign Program's Venue -- without this the
  feature is dead;
* it carries id and name ONLY -- no address, timezone, owner or legacy Program
  link, so it discloses a building's existence and nothing else about it;
* `venues` stays Season-ceilinged -- what a Season USES is unchanged, only what
  it may be OFFERED is widened;
* `rinks` and `ice_slots` do NOT follow -- the exception stops at the Venue;
* no competition-tree list widens.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = ("admin", Role.LEAGUE_ADMIN, {})


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        store = SqlStore(url)
        store.clear_all_data()
        yield "postgres", store


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _two_programs_with_facilities(api):
    """Two Programs, each with a Season, plus one Venue per Program with a
    Rink and an IceSlot under it -- so the exception's width can be measured on
    every level of the facility tree, not just its top."""
    out = {}
    for tag in ("A", "B"):
        program = api.create_program(f"FX Prog {tag}", "US", "UTC")
        season = api.create_season(program["id"], f"FX Season {tag}")
        org = api.create_organization(f"FX Org {tag}")
        venue = api.create_venue(f"FX-VENUE-{tag}", "", "UTC", org["id"], None)
        rink = api.create_rink(venue["id"], f"FX-RINK-{tag}")
        slot = api.create_ice_slot(rink["id"], f"2028-0{1 if tag == 'A' else 2}"
                                   "-01T10:00:00+00:00",
                                   f"2028-0{1 if tag == 'A' else 2}"
                                   "-01T11:00:00+00:00", "game")
        # The grant is what LINKS this Venue to this Program -- and what makes
        # it invisible to the other one under the ceiling alone.
        api.grant_season_venue_access(season["id"], venue["id"])
        out[tag] = {"program": program, "season": season, "org": org,
                    "venue": venue, "rink": rink, "slot": slot}
    return out


class FacilityTreeExceptionTest(unittest.TestCase):

    def test_a_foreign_venue_is_grantable_but_nothing_else_widens(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs_with_facilities(api)
                a, b = fx["A"], fx["B"]

                api.set_active_context(*ADMIN, a["program"]["id"],
                                       a["season"]["id"])
                ov = api.get_setup_overview_v2(*ADMIN)
                self.assertNotIn("error", ov, ov)

                grantable = {v["id"]: v for v in ov["grantable_venues"]}
                # 1. The feature: B's Venue is offerable while A is active.
                self.assertIn(
                    b["venue"]["id"], grantable,
                    f"[{backend}] Program B's Venue is not offerable from "
                    f"Program A, so the shared-arena grant deadlocks on its "
                    f"own first use")
                self.assertEqual(grantable[b["venue"]["id"]]["name"],
                                 "FX-VENUE-B", backend)

                # 2. ...carrying a building's id and name and NOTHING else.
                self.assertEqual(
                    set(grantable[b["venue"]["id"]]), {"id", "name"},
                    f"[{backend}] grantable_venues leaked fields beyond "
                    f"id+name: {grantable[b['venue']['id']]}")

                # 3. `venues` itself stays Season-ceilinged.
                scoped_venue_ids = {v["id"] for v in ov["venues"]}
                self.assertIn(a["venue"]["id"], scoped_venue_ids, backend)
                self.assertNotIn(
                    b["venue"]["id"], scoped_venue_ids,
                    f"[{backend}] the exception widened `venues` itself -- "
                    f"what a Season USES must stay ceilinged; only what it may "
                    f"be OFFERED was widened")

                # 4. The exception STOPS at the Venue: no foreign Rink, no
                #    foreign IceSlot, on any list.
                self.assertNotIn(b["rink"]["id"],
                                 {r["id"] for r in ov["rinks"]},
                                 f"[{backend}] a foreign Rink came with it")
                self.assertNotIn(b["slot"]["id"],
                                 {s["id"] for s in ov["ice_slots"]},
                                 f"[{backend}] a foreign IceSlot came with it")
                self.assertNotIn("FX-RINK-B", str(ov),
                                 f"[{backend}] a foreign Rink NAME leaked")

                # 5. No competition-tree list widened.
                self.assertNotIn(b["program"]["id"],
                                 {p["id"] for p in ov["programs"]}, backend)
                self.assertNotIn(b["season"]["id"],
                                 {s["id"] for s in ov["seasons"]}, backend)
                self.assertNotIn(
                    b["org"]["id"],
                    {o["id"] for o in ov["organizations"]}
                    | {o["id"] for o in ov["pending_link_organizations"]},
                    f"[{backend}] the foreign Venue's OWNER organization came "
                    f"with it -- the exception is the Venue, not its owner")

                # 6. It flips with the context rather than being additive.
                api.set_active_context(*ADMIN, b["program"]["id"],
                                       b["season"]["id"])
                ov_b = api.get_setup_overview_v2(*ADMIN)
                self.assertNotIn(a["venue"]["id"],
                                 {v["id"] for v in ov_b["venues"]}, backend)
                self.assertIn(a["venue"]["id"],
                              {v["id"] for v in ov_b["grantable_venues"]},
                              backend)
                _close(store)

    def test_another_operators_UNLINKED_venue_is_not_grantable(self):
        """The exception covers established facilities, not private drafts.

        Written after the first version of this exception returned every Venue
        in the installation and leaked one operator's never-linked arena to
        another on a Program-less install -- caught by
        `test_zero_program_bootstrap_scoping`. A Venue linked to no Program at
        all is governed by the creator-only `pending_link_venues` contract; the
        sharing exception must not reach around it.
        """
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _two_programs_with_facilities(api)
                a = fx["A"]
                # A third Venue belonging to NOBODY's Program, created by a
                # DIFFERENT account than the one reading below.
                other_org = api.create_organization("FX Draft Org",
                                                    actor_id="user_other")
                draft = api.create_venue("FX-DRAFT-VENUE", "", "UTC",
                                         other_org["id"], None,
                                         actor_id="user_other")

                api.set_active_context(*ADMIN, a["program"]["id"],
                                       a["season"]["id"])
                ov = api.get_setup_overview_v2(*ADMIN)
                grantable_ids = {v["id"] for v in ov["grantable_venues"]}
                self.assertNotIn(
                    draft["id"], grantable_ids,
                    f"[{backend}] another operator's never-linked Venue is "
                    f"offerable -- the sharing exception reached around the "
                    f"creator-only pending-link contract")
                self.assertNotIn(
                    "FX-DRAFT-VENUE", str(ov),
                    f"[{backend}] its NAME leaked somewhere in the payload")
                # ...while the genuinely shared, linked one still is.
                self.assertIn(fx["B"]["venue"]["id"], grantable_ids, backend)
                _close(store)

    def test_the_legacy_unfiltered_form_gains_nothing(self):
        """`role=None` already returns every Venue in `venues`, so the
        exception must not also duplicate them into a second list that internal
        callers would then have to know about."""
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                _two_programs_with_facilities(api)
                ov = api.get_setup_overview_v2()
                self.assertEqual(
                    ov["grantable_venues"], [],
                    f"[{backend}] the unfiltered form grew a redundant "
                    f"grantable_venues: {ov['grantable_venues']}")
                self.assertEqual(len(ov["venues"]), 2, backend)
                _close(store)
