"""The grant-only facility contract (#369 review).

Cross-Program venue sharing needs ONE reach across the Program ceiling: an
arena serves several leagues, and the moment Program A takes a grant the Venue
is linked to A, so under the ceiling alone no other Program can ever obtain the
grant that would make it visible. The capability deadlocks on its own first use.

The first attempt satisfied that by adding `grantable_venues` to
`get_setup_overview_v2`, and it failed review for a reason worth recording
here, because it is the trap this whole module exists to keep shut:

    `/api/v2/setup/overview` is gated MANAGE_ARENA; the grant POST needs
    MANAGE_SETUP. So an Arena Manager -- a role that cannot perform the sharing
    action at all -- received every linked Venue's id and name, independent of
    its active Program. The disclosure was not bounded to the feature needing
    it, and ordinary read visibility had become de-facto write authorization.

So the candidate list moved to its own MANAGE_SETUP-gated route,
`GET /api/v2/setup/seasons/<id>/venue-candidates`, bound to a valid destination
Season inside the caller's active Program. This module pins BOTH halves:

* the ordinary overview stays ceilinged and enumerates no foreign Venue, for
  every role; and
* the candidate contract is unavailable to roles that cannot grant, and returns
  only legitimate candidates to the role that can.

A third half arrived with the SAME shape and is pinned here too. Adding the
additive `venue_name` to `GET /api/v2/setup/seasons/<id>/venue-access` resolved
every returned `venue_id` through `all_venues()` on a route that had only a
role-level MANAGE_SETUP gate and took the requested Season id on trust. With
Program A active, asking for Program B's Season answered 200 with B's Venue id
AND name; a guessed Season id answered 200 with an empty list. One route, two
disclosures: a cross-Program facility name and a Season-existence oracle. The
lesson is identical to the candidate list's -- permission to manage SOME
Season's grants is not authorization for THIS Season -- so the fix is
identical: the read resolves the persisted active tuple and refuses a foreign
or nonexistent Season down one generic path, and `venue_name` is serialized
only past that check.
"""

import http.client
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

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


def _fixture(api):
    """Two Programs with facilities, plus every link shape the contract must
    distinguish: an ACTIVE grant, a REVOKED-ONLY grant, a LEGACY Program link,
    and another operator's UNLINKED draft."""
    out = {}
    for tag in ("A", "B"):
        program = api.create_program(f"FX Prog {tag}", "US", "UTC")
        season = api.create_season(program["id"], f"FX Season {tag}")
        org = api.create_organization(f"FX Org {tag}")
        venue = api.create_venue(f"FX-VENUE-{tag}", "", "UTC", org["id"], None)
        rink = api.create_rink(venue["id"], f"FX-RINK-{tag}")
        slot = api.create_ice_slot(rink["id"], "2028-01-01T10:00:00+00:00",
                                   "2028-01-01T11:00:00+00:00", "game")
        api.grant_season_venue_access(season["id"], venue["id"])
        out[tag] = {"program": program, "season": season, "org": org,
                    "venue": venue, "rink": rink, "slot": slot}

    # REVOKED-ONLY: linked to Program B by history alone. Revoking deactivates
    # rather than deletes, so this is still an established facility.
    revoked = api.create_venue("FX-REVOKED-VENUE", "", "UTC",
                               out["B"]["org"]["id"], None)
    grant = api.grant_season_venue_access(out["B"]["season"]["id"],
                                          revoked["id"])
    api.revoke_season_venue_access(grant["id"] if isinstance(grant, dict)
                                   else grant.id)
    out["revoked"] = revoked

    # LEGACY Program link: Venue.league_id stores a PROGRAM id despite its name.
    out["legacy"] = api.create_venue("FX-LEGACY-VENUE", "", "UTC", None,
                                     out["B"]["program"]["id"])

    # Another operator's UNLINKED draft -- never a candidate for anyone else.
    out["draft"] = api.create_venue("FX-DRAFT-VENUE", "", "UTC", None, None,
                                    actor_id="user_someone_else")
    return out


# -- the venue-access READ (#369 review, second finding) --------------------

PROBE = "VA-PROBE-ONLY-B-RINK"     # named ONLY by Program B; the disclosure probe
SHARED = "VA-SHARED-ARENA"         # named by BOTH; the legitimate cross-Program name


def _access_fixture(api):
    """Two genuinely distinguishable Programs, plus every grant shape the
    scoped read must still serve for the caller's OWN Season.

    Program A's Season holds three grants: ACTIVE to its own home rink, REVOKED
    to a retired one (revoke deactivates rather than deletes, so the row must
    still come back), and ACTIVE to an arena that belongs to Program B's
    organization and that B also holds -- the legitimately shared facility the
    additive ``venue_name`` exists to name.

    Program B's Season additionally holds a grant to ``PROBE``, a Venue Program
    A never touches. Nothing A may legitimately read names it, so its id and
    name are the probe: if either appears in a refusal, the route is disclosing
    another Program's facilities.
    """
    out = {}
    for tag in ("A", "B"):
        program = api.create_program(f"VA Prog {tag}", "US", "UTC")
        season = api.create_season(program["id"], f"VA Season {tag}")
        org = api.create_organization(f"VA Org {tag}")
        out[tag] = {"program": program, "season": season, "org": org}

    out["home"] = api.create_venue("VA-HOME-RINK-A", "", "UTC",
                                   out["A"]["org"]["id"], None)
    api.grant_season_venue_access(out["A"]["season"]["id"], out["home"]["id"])

    out["retired"] = api.create_venue("VA-RETIRED-RINK-A", "", "UTC",
                                      out["A"]["org"]["id"], None)
    gone = api.grant_season_venue_access(out["A"]["season"]["id"],
                                         out["retired"]["id"])
    api.revoke_season_venue_access(gone["id"])

    out["shared"] = api.create_venue(SHARED, "", "UTC",
                                     out["B"]["org"]["id"], None)
    api.grant_season_venue_access(out["B"]["season"]["id"], out["shared"]["id"])
    api.grant_season_venue_access(out["A"]["season"]["id"], out["shared"]["id"])

    out["probe"] = api.create_venue(PROBE, "", "UTC", out["B"]["org"]["id"],
                                    None)
    api.grant_season_venue_access(out["B"]["season"]["id"], out["probe"]["id"])
    return out


def _mask(raw: bytes, requested_id: str) -> bytes:
    """A refusal's raw bytes with ONLY the echoed request id masked.

    The caller supplied that id, so echoing it discloses nothing; everything
    else in the two bodies must be byte-identical, or the difference is exactly
    the signal that tells a foreign Season apart from a nonexistent one.
    """
    return raw.replace(requested_id.encode(), b"<REQUESTED_SEASON>")


class ScopedVenueAccessReadTest(unittest.TestCase):
    """Half three: the grant READ is ceilinged like the candidate read."""

    def _assert_fixture_is_distinguishable(self, store, fx, backend):
        """The precondition, asserted BEFORE any refusal.

        Without this the refusal assertions below pass against a fixture whose
        Seasons share a Program (nothing to refuse) or whose Venues share a
        name (nothing to detect) -- i.e. they would pass with the fix reverted.
        """
        a, b = fx["A"], fx["B"]
        self.assertNotEqual(a["program"]["id"], b["program"]["id"], backend)
        season_b = store.get_season(b["season"]["id"])
        self.assertIsNotNone(season_b, f"[{backend}] Program B's Season is missing")
        self.assertEqual(season_b.program_id, b["program"]["id"], backend)
        self.assertNotEqual(
            season_b.program_id, a["program"]["id"],
            f"[{backend}] the two Seasons share a Program, so there is no "
            f"cross-Program read to refuse")
        # The probe Venue is named distinctly from everything A may see...
        self.assertNotIn(
            fx["probe"]["name"],
            {fx["home"]["name"], fx["retired"]["name"], fx["shared"]["name"]},
            f"[{backend}] the probe Venue's name is not distinguishable from "
            f"a Venue Program A legitimately names")
        # ...really is granted to B...
        b_grants = {g.venue_id for g in
                    store.season_venue_access_for_season(b["season"]["id"])}
        self.assertIn(fx["probe"]["id"], b_grants,
                      f"[{backend}] the probe Venue is not actually granted to "
                      f"Program B, so reading B could not have named it anyway")
        # ...and is reachable from NO grant Program A holds.
        a_grants = {g.venue_id for g in
                    store.season_venue_access_for_season(a["season"]["id"])}
        self.assertNotIn(fx["probe"]["id"], a_grants, backend)

    def test_a_foreign_or_nonexistent_season_is_refused_identically(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _access_fixture(api)
                a, b = fx["A"], fx["B"]
                self._assert_fixture_is_distinguishable(store, fx, backend)

                api.set_active_context(*ADMIN, a["program"]["id"],
                                       a["season"]["id"])
                grants_before = {g.id: g.active
                                 for g in store.all_season_venue_access()}
                audit_before = len(store.all_setup_audit())

                foreign = api.list_season_venue_access(b["season"]["id"], *ADMIN)
                missing = api.list_season_venue_access("season_never_existed",
                                                       *ADMIN)
                self.assertIn("error", foreign,
                              f"[{backend}] Program B's Season was readable "
                              f"while Program A was active: {foreign}")
                self.assertIn("error", missing, missing)

                # Raw BYTES, masking only the echoed id: a foreign Season and a
                # nonexistent one must be literally the same answer.
                raw_f = _mask(json.dumps(foreign, sort_keys=True).encode(),
                              b["season"]["id"])
                raw_m = _mask(json.dumps(missing, sort_keys=True).encode(),
                              "season_never_existed")
                self.assertEqual(
                    raw_f, raw_m,
                    f"[{backend}] a foreign Season is distinguishable from a "
                    f"nonexistent one: {raw_f!r} vs {raw_m!r}")

                # Neither refusal names anything of Program B's.
                for label, raw, rid in (("foreign", raw_f, b["season"]["id"]),
                                        ("missing", raw_m,
                                         "season_never_existed")):
                    for token in (b["season"]["id"], b["season"]["name"],
                                  b["program"]["id"], fx["probe"]["id"],
                                  fx["probe"]["name"], fx["shared"]["id"],
                                  fx["shared"]["name"]):
                        self.assertNotIn(
                            token.encode(), raw,
                            f"[{backend}/{label}] the refusal disclosed "
                            f"{token!r}")

                # An identity-less call cannot walk around the ceiling either.
                anon = api.list_season_venue_access(b["season"]["id"])
                self.assertIn("error", anon, anon)
                self.assertEqual(anon["error"]["code"], "forbidden", anon)
                self.assertNotIn(fx["probe"]["name"], json.dumps(anon), backend)

                # A refused read is a READ: no grant and no audit row moved.
                self.assertEqual(
                    {g.id: g.active for g in store.all_season_venue_access()},
                    grants_before, f"[{backend}] a refused read changed grants")
                self.assertEqual(len(store.all_setup_audit()), audit_before,
                                 f"[{backend}] a refused read wrote an audit row")
                _close(store)

    def test_the_active_programs_own_season_still_names_its_venues(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _access_fixture(api)
                a, b = fx["A"], fx["B"]
                self._assert_fixture_is_distinguishable(store, fx, backend)
                # The shared arena really is cross-Program: B holds it too, so
                # naming it below is the wanted additive behavior, not a leak.
                self.assertIn(
                    fx["shared"]["id"],
                    {g.venue_id for g in
                     store.season_venue_access_for_season(b["season"]["id"])},
                    f"[{backend}] the 'shared' arena is not actually shared "
                    f"with Program B, so this proves nothing about sharing")

                api.set_active_context(*ADMIN, a["program"]["id"],
                                       a["season"]["id"])
                res = api.list_season_venue_access(a["season"]["id"], *ADMIN)
                self.assertNotIn("error", res, res)
                rows = {r["venue_id"]: r for r in res["venue_access"]}
                self.assertEqual(
                    set(rows), {fx["home"]["id"], fx["retired"]["id"],
                                fx["shared"]["id"]}, (backend, rows))
                # Active AND revoked rows, each additively named.
                self.assertTrue(rows[fx["home"]["id"]]["active"], backend)
                self.assertFalse(
                    rows[fx["retired"]["id"]]["active"],
                    f"[{backend}] the revoked grant row vanished from the "
                    f"listing the cleanup surface depends on")
                self.assertTrue(rows[fx["shared"]["id"]]["active"], backend)
                self.assertEqual(rows[fx["home"]["id"]]["venue_name"],
                                 "VA-HOME-RINK-A", backend)
                self.assertEqual(rows[fx["retired"]["id"]]["venue_name"],
                                 "VA-RETIRED-RINK-A", backend)
                self.assertEqual(
                    rows[fx["shared"]["id"]]["venue_name"], SHARED,
                    f"[{backend}] a legitimately shared cross-Program arena "
                    f"lost its name -- the Allowed-venues row is a bare id "
                    f"again, which is the defect venue_name was added to fix")

                # Program B's exclusive facility rides along nowhere.
                blob = json.dumps(res)
                for token in (fx["probe"]["id"], fx["probe"]["name"],
                              b["season"]["id"]):
                    self.assertNotIn(token, blob, (backend, token))
                _close(store)


# -- the EXACT selected-Season ceiling (#369 OWNER RULING) ------------------
#
# Both routes above first ceilinged on the PROGRAM: the requested Season had
# only to satisfy `season.program_id == active_program.id`. The repository
# owner ruled that insufficient --
#
#     "Use the exact selected-Season ceiling. The same-Program/other-Season
#     200 remains incorrect. Both the venue-access list and candidate route
#     must require the requested Season to equal the persisted selected
#     Season; Records must stop using the endpoint as an all-Seasons
#     inventory."
#
# -- so with Program A active and Season A1 SELECTED, asking either route for
# Season A2 (a sibling Season of that very Program) must now be refused down
# the same generic path as a foreign or nonexistent Season. A Program-only
# context has no selected Season for a request to equal, so it fails closed
# too. The all-Seasons client walk that motivated the Program ceiling is gone:
# `app.js` reads both routes for the selected Season alone and renders a
# non-identifying placeholder for the rest.
#
# What the ruling did NOT touch is which Venues may be OFFERED. The ceiling is
# the DESTINATION Season; the cross-Program venue-sharing exception stands, so
# a Venue linked only to another Program is still a candidate and a shared
# arena the selected Season already holds is still named. The success cases
# below pin exactly that, or "tighten the ceiling" would be indistinguishable
# from "delete the feature".

CEIL_SHARED = "CEIL-SHARED-ARENA"      # Org B's, held by BOTH Programs' Seasons
CEIL_CROSS = "CEIL-CROSS-ONLY-B"       # Org B's, held by B alone: a candidate
CEIL_SIBLING = "CEIL-SIBLING-ONLY-A2"  # Org A's, held by A's OTHER Season alone


def _ceiling_fixture(api):
    """Program A with TWO Seasons, Program B with one, and every Venue shape
    the ruling distinguishes.

    Season A1 is the one that will be SELECTED. It holds an ACTIVE grant to its
    own home rink, a REVOKED grant to a retired one, and an ACTIVE grant to
    ``CEIL_SHARED`` -- an arena owned by Program B's organization that Program
    B's Season holds too, i.e. a genuinely shared facility.

    Season A2 is the ruling's headline case: same Program, never selected. It
    alone holds ``CEIL_SIBLING``, so that Venue's id and name are exactly what
    a same-Program/other-Season 200 would have disclosed.

    ``CEIL_CROSS`` is Program B's alone and is granted nowhere in Program A --
    the legitimate cross-Program CANDIDATE, present so that tightening the
    destination ceiling can be told apart from quietly filtering the candidate
    set back to the active Program (which would re-deadlock arena sharing).
    """
    out = {}
    for tag in ("A", "B"):
        out[tag] = {
            "program": api.create_program(f"CEIL Prog {tag}", "US", "UTC"),
            "org": api.create_organization(f"CEIL Org {tag}"),
        }
    out["A"]["season"] = api.create_season(out["A"]["program"]["id"],
                                           "CEIL Season A1")
    out["A"]["sibling"] = api.create_season(out["A"]["program"]["id"],
                                            "CEIL Season A2")
    out["B"]["season"] = api.create_season(out["B"]["program"]["id"],
                                           "CEIL Season B1")
    sel = out["A"]["season"]["id"]

    out["home"] = api.create_venue("CEIL-HOME-RINK-A1", "", "UTC",
                                   out["A"]["org"]["id"], None)
    api.grant_season_venue_access(sel, out["home"]["id"])

    out["retired"] = api.create_venue("CEIL-RETIRED-RINK-A1", "", "UTC",
                                      out["A"]["org"]["id"], None)
    gone = api.grant_season_venue_access(sel, out["retired"]["id"])
    api.revoke_season_venue_access(gone["id"])

    out["shared"] = api.create_venue(CEIL_SHARED, "", "UTC",
                                     out["B"]["org"]["id"], None)
    api.grant_season_venue_access(out["B"]["season"]["id"], out["shared"]["id"])
    api.grant_season_venue_access(sel, out["shared"]["id"])

    out["cross"] = api.create_venue(CEIL_CROSS, "", "UTC",
                                    out["B"]["org"]["id"], None)
    api.grant_season_venue_access(out["B"]["season"]["id"], out["cross"]["id"])

    out["sibling_venue"] = api.create_venue(CEIL_SIBLING, "", "UTC",
                                            out["A"]["org"]["id"], None)
    api.grant_season_venue_access(out["A"]["sibling"]["id"],
                                  out["sibling_venue"]["id"])
    return out


class SelectedSeasonCeilingTest(unittest.TestCase):
    """The owner ruling, on every store backend, for BOTH facade methods."""

    def _methods(self, api):
        """Both contracts under one name, so no case can cover only one of
        them -- the ruling names them together and they drifted apart once
        already."""
        return (("venue-access", api.list_season_venue_access),
                ("candidates", api.get_venue_grant_candidates))

    def _assert_preconditions(self, store, fx, backend):
        """Asserted BEFORE any refusal, because every refusal assertion below
        is vacuous without them.

        If the "sibling" Season were not in the selected Season's Program there
        would be no same-Program/other-Season case at all; if the "shared"
        arena were not genuinely held by both Programs, naming it would prove
        nothing about sharing; if the Venue names collided, no disclosure could
        be detected. Each of these would let the whole class pass with the fix
        reverted.
        """
        a, b = fx["A"], fx["B"]
        sel = store.get_season(a["season"]["id"])
        sib = store.get_season(a["sibling"]["id"])
        foreign = store.get_season(b["season"]["id"])
        for label, row in (("selected", sel), ("sibling", sib),
                           ("foreign", foreign)):
            self.assertIsNotNone(row, f"[{backend}] the {label} Season is missing")
        self.assertNotEqual(sib.id, sel.id, backend)
        self.assertEqual(
            sib.program_id, sel.program_id,
            f"[{backend}] the 'sibling' Season is not in the SELECTED Season's "
            f"Program, so this fixture never exercises the same-Program/"
            f"other-Season 200 the owner ruled incorrect")
        self.assertNotEqual(
            foreign.program_id, sel.program_id,
            f"[{backend}] the 'foreign' Season shares the active Program, so "
            f"there is no cross-Program read left to refuse")

        grants = {key: {g.venue_id for g in
                        store.season_venue_access_for_season(sid)}
                  for key, sid in (("sel", sel.id), ("sib", sib.id),
                                   ("foreign", foreign.id))}
        self.assertIn(fx["shared"]["id"], grants["sel"], backend)
        self.assertIn(
            fx["shared"]["id"], grants["foreign"],
            f"[{backend}] the 'shared' arena is held by one Program only, so "
            f"naming it proves nothing about cross-Program sharing")
        self.assertIn(
            fx["sibling_venue"]["id"], grants["sib"],
            f"[{backend}] the sibling Season holds no Venue of its own, so "
            f"reading it could not have disclosed one anyway")
        self.assertNotIn(fx["sibling_venue"]["id"], grants["sel"], backend)
        self.assertIn(fx["cross"]["id"], grants["foreign"], backend)
        self.assertNotIn(fx["cross"]["id"], grants["sel"], backend)
        names = [fx[k]["name"] for k in
                 ("home", "retired", "shared", "cross", "sibling_venue")]
        self.assertEqual(
            len(set(names)), len(names),
            f"[{backend}] two fixture Venues share a name, so a disclosure "
            f"could not be told from a legitimate row: {names}")

    def _tokens(self, fx):
        """Everything a refusal must never name."""
        return (fx["A"]["sibling"]["id"], fx["A"]["sibling"]["name"],
                fx["B"]["season"]["id"], fx["B"]["season"]["name"],
                fx["B"]["program"]["id"],
                fx["sibling_venue"]["id"], fx["sibling_venue"]["name"],
                fx["cross"]["id"], fx["cross"]["name"],
                fx["shared"]["id"], fx["shared"]["name"])

    def test_the_selected_season_still_serves_both_contracts(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _ceiling_fixture(api)
                self._assert_preconditions(store, fx, backend)
                sel = fx["A"]["season"]["id"]
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"], sel)

                listing = api.list_season_venue_access(sel, *ADMIN)
                self.assertNotIn("error", listing, listing)
                rows = {r["venue_id"]: r for r in listing["venue_access"]}
                self.assertEqual(
                    set(rows), {fx["home"]["id"], fx["retired"]["id"],
                                fx["shared"]["id"]}, (backend, rows))
                self.assertTrue(rows[fx["home"]["id"]]["active"], backend)
                self.assertFalse(
                    rows[fx["retired"]["id"]]["active"],
                    f"[{backend}] the revoked grant row vanished from the "
                    f"listing the cleanup surface depends on")
                self.assertEqual(rows[fx["home"]["id"]]["venue_name"],
                                 "CEIL-HOME-RINK-A1", backend)
                self.assertEqual(rows[fx["retired"]["id"]]["venue_name"],
                                 "CEIL-RETIRED-RINK-A1", backend)
                self.assertEqual(
                    rows[fx["shared"]["id"]]["venue_name"], CEIL_SHARED,
                    f"[{backend}] a legitimately shared cross-Program arena "
                    f"lost its name -- the Allowed-venues row is a bare id "
                    f"again, which is the defect venue_name was added to fix")
                blob = json.dumps(listing)
                for token in (fx["sibling_venue"]["id"],
                              fx["sibling_venue"]["name"],
                              fx["cross"]["id"], fx["cross"]["name"],
                              fx["A"]["sibling"]["id"],
                              fx["B"]["season"]["id"]):
                    self.assertNotIn(token, blob, (backend, token))

                cand = api.get_venue_grant_candidates(sel, *ADMIN)
                self.assertNotIn("error", cand, cand)
                ids = {c["id"] for c in cand["candidates"]}
                self.assertIn(
                    fx["cross"]["id"], ids,
                    f"[{backend}] a Venue linked only to ANOTHER Program is no "
                    f"longer a candidate -- tightening the destination Season "
                    f"must not have narrowed the candidate SET, or arena "
                    f"sharing deadlocks on its own first use again")
                self.assertIn(
                    fx["sibling_venue"]["id"], ids,
                    f"[{backend}] a Venue established by a sibling Season's "
                    f"grant is not offerable to the selected Season")
                self.assertIn(fx["retired"]["id"], ids, backend)
                # Already actively granted here, so not a CANDIDATE.
                self.assertNotIn(fx["home"]["id"], ids, backend)
                self.assertNotIn(fx["shared"]["id"], ids, backend)
                for c in cand["candidates"]:
                    self.assertEqual(set(c), {"id", "name"}, (backend, c))
                cand_blob = json.dumps(cand)
                for token in (fx["A"]["sibling"]["id"], fx["B"]["season"]["id"]):
                    self.assertNotIn(token, cand_blob, (backend, token))
                _close(store)

    def test_a_sibling_season_is_refused_exactly_like_a_missing_one(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _ceiling_fixture(api)
                self._assert_preconditions(store, fx, backend)
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                grants_before = {g.id: g.active
                                 for g in store.all_season_venue_access()}
                audit_before = len(store.all_setup_audit())

                for name, call in self._methods(api):
                    cases = {
                        # The ruling's headline case, first.
                        "sibling": fx["A"]["sibling"]["id"],
                        "foreign": fx["B"]["season"]["id"],
                        "missing": "season_never_existed",
                    }
                    raws = {}
                    for label, sid in cases.items():
                        res = call(sid, *ADMIN)
                        self.assertIn(
                            "error", res,
                            f"[{backend}/{name}] the {label} Season was "
                            f"readable while another Season was selected: "
                            f"{res}")
                        raws[label] = _mask(
                            json.dumps(res, sort_keys=True).encode(), sid)
                    self.assertEqual(
                        raws["sibling"], raws["missing"],
                        f"[{backend}/{name}] a SIBLING Season of the active "
                        f"Program is distinguishable from a nonexistent one: "
                        f"{raws['sibling']!r} vs {raws['missing']!r}")
                    self.assertEqual(
                        raws["foreign"], raws["missing"],
                        f"[{backend}/{name}] a foreign-Program Season is "
                        f"distinguishable from a nonexistent one: "
                        f"{raws['foreign']!r} vs {raws['missing']!r}")
                    for label, raw in raws.items():
                        for token in self._tokens(fx):
                            self.assertNotIn(
                                token.encode(), raw,
                                f"[{backend}/{name}/{label}] the refusal "
                                f"disclosed {token!r}")

                # Every refusal above was a READ: nothing moved.
                self.assertEqual(
                    {g.id: g.active for g in store.all_season_venue_access()},
                    grants_before, f"[{backend}] a refused read changed grants")
                self.assertEqual(
                    len(store.all_setup_audit()), audit_before,
                    f"[{backend}] a refused read wrote an audit row")
                _close(store)

    def test_a_program_only_context_fails_closed_for_both(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _ceiling_fixture(api)
                self._assert_preconditions(store, fx, backend)
                # Program-only: a real, first-class selection, not an error.
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"], None)
                ctx = api.get_active_context(*ADMIN)
                self.assertEqual(ctx["program_id"], fx["A"]["program"]["id"],
                                 (backend, ctx))
                self.assertIsNone(
                    ctx["season_id"],
                    f"[{backend}] the context resolved to a Season, so this "
                    f"case never exercises the Program-only state: {ctx}")
                grants_before = {g.id: g.active
                                 for g in store.all_season_venue_access()}
                audit_before = len(store.all_setup_audit())

                for name, call in self._methods(api):
                    own = call(fx["A"]["season"]["id"], *ADMIN)
                    missing = call("season_never_existed", *ADMIN)
                    self.assertIn(
                        "error", own,
                        f"[{backend}/{name}] a Season of the active Program "
                        f"was readable with NO Season selected -- there is no "
                        f"selected Season for the request to equal: {own}")
                    self.assertIn("error", missing, missing)
                    raw_own = _mask(json.dumps(own, sort_keys=True).encode(),
                                    fx["A"]["season"]["id"])
                    raw_missing = _mask(
                        json.dumps(missing, sort_keys=True).encode(),
                        "season_never_existed")
                    self.assertEqual(
                        raw_own, raw_missing,
                        f"[{backend}/{name}] failing closed is distinguishable "
                        f"from a nonexistent Season: {raw_own!r} vs "
                        f"{raw_missing!r}")
                    for token in self._tokens(fx) + (fx["home"]["id"],
                                                     fx["home"]["name"]):
                        self.assertNotIn(token.encode(), raw_own,
                                         f"[{backend}/{name}] {token!r}")

                self.assertEqual(
                    {g.id: g.active for g in store.all_season_venue_access()},
                    grants_before, f"[{backend}] a refused read changed grants")
                self.assertEqual(
                    len(store.all_setup_audit()), audit_before,
                    f"[{backend}] a refused read wrote an audit row")
                _close(store)


class OrdinaryOverviewStaysCeilingedTest(unittest.TestCase):
    """Half one: the ordinary read discloses no foreign Venue, to ANY role."""

    def test_no_role_sees_a_foreign_venue_in_the_ordinary_overview(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                # Program B active, per the reviewer's scenario.
                exercised = []
                for label, ident in (
                        ("league_admin", ("admin", Role.LEAGUE_ADMIN, {})),
                        ("arena_manager", ("arena", Role.ARENA_MANAGER, {})),
                        ("zero_scope", ("nobody", Role.ARENA_MANAGER,
                                        {"program_id": "program_missing"}))):
                    # Context is PER USER, so it must be set for the very
                    # identity that then reads -- setting it as the admin and
                    # reading as the Arena Manager left that caller resolving
                    # to Program A and proved nothing.
                    api.set_active_context(*ident,
                                           fx["B"]["program"]["id"],
                                           fx["B"]["season"]["id"])
                    ov = api.get_setup_overview_v2(*ident)
                    if "error" in ov:
                        continue          # a denied identity discloses nothing
                    exercised.append(label)
                    blob = json.dumps(ov)
                    self.assertNotIn(
                        "grantable_venues", blob,
                        f"[{backend}/{label}] the ordinary overview grew the "
                        f"candidate list back -- that is the reviewed defect")
                    for token in ("FX-VENUE-A", fx["A"]["venue"]["id"],
                                  "FX-DRAFT-VENUE", fx["draft"]["id"]):
                        self.assertNotIn(
                            token, blob,
                            f"[{backend}/{label}] the ordinary overview "
                            f"enumerated {token!r} while Program B was active")
                # Non-vacuity: at least the two operator roles must actually
                # have produced a payload, or the `continue` above would let
                # this pass without reading anything.
                self.assertIn("league_admin", exercised, backend)
                self.assertIn(
                    "arena_manager", exercised,
                    f"[{backend}] the Arena Manager never produced an "
                    f"overview, so its ceiling was never checked")
                _close(store)


class GrantCandidateContractTest(unittest.TestCase):
    """Half two: the candidate set itself."""

    def test_candidates_are_established_facilities_and_own_drafts_only(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                res = api.get_venue_grant_candidates(fx["A"]["season"]["id"],
                                                     *ADMIN)
                self.assertNotIn("error", res, res)
                ids = {c["id"] for c in res["candidates"]}

                # IN: another Program's active-granted Venue (the feature),
                # its revoked-only Venue, and a legacy Program-linked one.
                self.assertIn(fx["B"]["venue"]["id"], ids,
                              f"[{backend}] sharing deadlocks: Program B's "
                              f"arena is not a candidate")
                self.assertIn(fx["revoked"]["id"], ids,
                              f"[{backend}] a revoked-only grant still ties "
                              f"the Venue to a Program; it stays a candidate")
                self.assertIn(fx["legacy"]["id"], ids,
                              f"[{backend}] the legacy Program link was missed")

                # OUT: another operator's unlinked draft, and the Venue this
                # Season already holds (it is not a CANDIDATE, it is granted).
                self.assertNotIn(
                    fx["draft"]["id"], ids,
                    f"[{backend}] another operator's never-linked draft is "
                    f"offerable -- the exception reached around the "
                    f"creator-only pending-link contract")
                self.assertNotIn(fx["A"]["venue"]["id"], ids, backend)

                # Candidates carry a building's id and name and nothing else.
                for c in res["candidates"]:
                    self.assertEqual(set(c), {"id", "name"}, (backend, c))

                # No Rink or IceSlot rides along.
                blob = json.dumps(res)
                self.assertNotIn(fx["B"]["rink"]["id"], blob, backend)
                self.assertNotIn(fx["B"]["slot"]["id"], blob, backend)
                _close(store)

    def test_a_foreign_or_nonexistent_season_is_refused_identically(self):
        for backend, store in _backends():
            with self.subTest(backend=backend):
                api = ApiService(store)
                fx = _fixture(api)
                api.set_active_context(*ADMIN, fx["A"]["program"]["id"],
                                       fx["A"]["season"]["id"])
                foreign = api.get_venue_grant_candidates(
                    fx["B"]["season"]["id"], *ADMIN)
                missing = api.get_venue_grant_candidates(
                    "season_never_existed", *ADMIN)
                self.assertIn("error", foreign, foreign)
                self.assertIn("error", missing, missing)
                self.assertEqual(
                    foreign["error"]["code"], missing["error"]["code"],
                    f"[{backend}] an inaccessible Season is distinguishable "
                    f"from a nonexistent one: {foreign} vs {missing}")
                # ...and neither refusal names anything.
                for res in (foreign, missing):
                    for token in ("FX-VENUE-B", "FX Season B"):
                        self.assertNotIn(token, json.dumps(res),
                                         (backend, res))
                _close(store)


class GrantCandidateHttpPermissionTest(unittest.TestCase):
    """The permission boundary, which only exists at the route.

    A facade test passes `role` by hand and would agree with itself even if the
    route were gated differently -- and the reviewed defect was precisely a
    mismatch between two routes' gates, so this is the layer that matters.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException) as e:
            return 0, json.dumps({"transport_error": repr(e)}).encode()

    def _login(self, username, password="demo"):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": username, "password": password})
        self.assertEqual(status, 200)
        return c

    def test_the_candidate_route_is_closed_to_roles_that_cannot_grant(self):
        admin = self._login("admin")
        status, raw = self._req(admin, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, raw[:200])
        ov = json.loads(raw)
        season_id = ov["seasons"][0]["id"]

        # The grant-capable role gets the contract...
        status, raw = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("candidates", json.loads(raw))

        # ...and the role that cannot grant is refused OUTRIGHT, rather than
        # receiving the same list through a more weakly gated route.
        arena = self._login("arena")
        status, raw = self._req(
            arena, "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(
            status, 403,
            f"an Arena Manager reached the grant-candidate contract: "
            f"{raw[:300]!r}")
        self.assertIn(b"manage_setup", raw)

        # ...and its ordinary overview carries no candidate list at all.
        status, raw = self._req(arena, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200)
        self.assertNotIn(
            b"grantable_venues", raw,
            "the Arena Manager's ordinary overview still carries the "
            "candidate list -- the reviewed defect")

        # Unauthenticated is refused before anything is computed.
        status, _ = self._req(
            self._client(), "GET",
            f"/api/v2/setup/seasons/{season_id}/venue-candidates")
        self.assertEqual(status, 401)

    def test_a_refused_candidate_read_mutates_nothing(self):
        admin = self._login("admin")
        store = self.srv.STATE.api.store
        grants_before = {a.id for a in store.all_season_venue_access()}
        audit_before = len(store.all_setup_audit())

        arena = self._login("arena")
        self._req(arena, "GET",
                  "/api/v2/setup/seasons/season_1/venue-candidates")
        self._req(admin, "GET",
                  "/api/v2/setup/seasons/season_never_existed/venue-candidates")

        self.assertEqual({a.id for a in store.all_season_venue_access()},
                         grants_before, "a refused read created a grant")
        self.assertEqual(len(store.all_setup_audit()), audit_before,
                         "a refused read wrote an audit row")


class ScopedVenueAccessHttpTest(unittest.TestCase):
    """The venue-access read over the REAL transport, with a real session.

    The facade cases above pass ``role``/``scope`` by hand; only here is the
    route itself proven to thread the authenticated identity through instead of
    stopping at its role gate -- which is precisely what it failed to do.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException) as e:
            return 0, json.dumps({"transport_error": repr(e)}).encode()

    def _login(self, username, password="demo"):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": username, "password": password})
        self.assertEqual(status, 200)
        return c

    def _v2(self, c, entity, body):
        status, raw = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, raw[:300])
        return json.loads(raw)

    def _build(self, c, tag):
        org = self._v2(c, "organization",
                       {"name": f"HVA Org {tag}", "short_name": f"H{tag}"})
        program = self._v2(c, "program",
                           {"name": f"HVA Prog {tag}",
                            "operator_organization_id": org["id"]})
        season = self._v2(c, "season",
                          {"program_id": program["id"],
                           "name": f"HVA Season {tag}"})
        return org, program, season

    def _grant(self, c, season_id, venue_id):
        # The grant is a WRITE naming an EXISTING Season, so the Season end is
        # ceilinged on the caller's ACTIVE Season (#369 target authorization):
        # a grant made while a different Season is selected is refused with the
        # generic "Season <id> not found.". Select the destination Season
        # first, exactly as the UI's context bar does -- the production guard
        # is honoured here, never bypassed. Each test sets the context it is
        # actually about afterwards.
        program_id = self.srv.STATE.api.store.get_season(season_id).program_id
        status, raw = self._req(c, "POST", "/api/context",
                                {"program_id": program_id,
                                 "season_id": season_id})
        self.assertEqual(status, 200, raw[:300])
        status, raw = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season_id}/venue-access",
            {"venue_id": venue_id})
        self.assertEqual(status, 200, raw[:300])
        return json.loads(raw)

    def _fixture(self, c):
        org_a, prog_a, season_a = self._build(c, "A")
        org_b, prog_b, season_b = self._build(c, "B")
        home = self._v2(c, "venue", {"name": "HVA-HOME-RINK-A",
                                     "organization_id": org_a["id"]})
        self._grant(c, season_a["id"], home["id"])
        retired = self._v2(c, "venue", {"name": "HVA-RETIRED-RINK-A",
                                        "organization_id": org_a["id"]})
        gone = self._grant(c, season_a["id"], retired["id"])
        status, raw = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{gone['id']}/remove", {})
        self.assertEqual(status, 200, raw[:300])
        shared = self._v2(c, "venue", {"name": f"HVA-{SHARED}",
                                       "organization_id": org_b["id"]})
        self._grant(c, season_b["id"], shared["id"])
        self._grant(c, season_a["id"], shared["id"])
        probe = self._v2(c, "venue", {"name": f"HVA-{PROBE}",
                                      "organization_id": org_b["id"]})
        self._grant(c, season_b["id"], probe["id"])
        # ACTIVATE Program A, the reviewer's scenario.
        status, raw = self._req(c, "POST", "/api/context",
                                {"program_id": prog_a["id"],
                                 "season_id": season_a["id"]})
        self.assertEqual(status, 200, raw[:300])
        return {"prog_a": prog_a, "season_a": season_a, "prog_b": prog_b,
                "season_b": season_b, "home": home, "retired": retired,
                "shared": shared, "probe": probe}

    def _assert_distinguishable(self, fx):
        """Precondition: the two Seasons really are in different Programs, and
        the probe Venue really is B's alone and distinctly named. Without this
        the refusal assertions would pass with the fix reverted."""
        store = self.srv.STATE.api.store
        self.assertNotEqual(fx["prog_a"]["id"], fx["prog_b"]["id"])
        season_b = store.get_season(fx["season_b"]["id"])
        self.assertEqual(season_b.program_id, fx["prog_b"]["id"])
        self.assertNotEqual(
            season_b.program_id, fx["prog_a"]["id"],
            "both Seasons are in one Program: nothing to refuse")
        self.assertNotIn(fx["probe"]["name"],
                         {fx["home"]["name"], fx["retired"]["name"],
                          fx["shared"]["name"]})
        b_grants = {g.venue_id for g in
                    store.season_venue_access_for_season(fx["season_b"]["id"])}
        self.assertIn(fx["probe"]["id"], b_grants,
                      "the probe Venue is not granted to B, so reading B could "
                      "not have disclosed it in the first place")
        a_grants = {g.venue_id for g in
                    store.season_venue_access_for_season(fx["season_a"]["id"])}
        self.assertNotIn(fx["probe"]["id"], a_grants)
        # And the active context really is A's.
        status, raw = self._req(self._admin_ref, "GET", "/api/context")
        self.assertEqual(status, 200, raw[:300])
        ctx = json.loads(raw)
        self.assertEqual((ctx.get("program") or {}).get("id"),
                         fx["prog_a"]["id"], raw[:300])

    def test_over_http_a_foreign_and_a_nonexistent_season_answer_identically(self):
        admin = self._admin_ref = self._login("admin")
        fx = self._fixture(admin)
        self._assert_distinguishable(fx)
        store = self.srv.STATE.api.store
        grants_before = {g.id: g.active for g in store.all_season_venue_access()}
        audit_before = len(store.all_setup_audit())

        st_f, body_f = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{fx['season_b']['id']}/venue-access")
        st_m, body_m = self._req(
            admin, "GET",
            "/api/v2/setup/seasons/season_never_existed/venue-access")

        self.assertEqual(
            st_f, 404,
            f"Program B's Season answered {st_f} while Program A was active: "
            f"{body_f[:300]!r}")
        self.assertEqual(
            st_f, st_m,
            f"status distinguishes a foreign Season ({st_f}) from a "
            f"nonexistent one ({st_m})")
        raw_f = _mask(body_f, fx["season_b"]["id"])
        raw_m = _mask(body_m, "season_never_existed")
        self.assertEqual(
            raw_f, raw_m,
            f"raw bodies differ beyond the echoed id: {raw_f!r} vs {raw_m!r}")
        for label, raw in (("foreign", raw_f), ("missing", raw_m)):
            for token in (fx["season_b"]["id"], fx["season_b"]["name"],
                          fx["prog_b"]["id"], fx["probe"]["id"],
                          fx["probe"]["name"], fx["shared"]["id"],
                          fx["shared"]["name"]):
                self.assertNotIn(token.encode(), raw,
                                 f"[{label}] the refusal disclosed {token!r}")

        self.assertEqual({g.id: g.active
                          for g in store.all_season_venue_access()},
                         grants_before, "a refused read changed grants")
        self.assertEqual(len(store.all_setup_audit()), audit_before,
                         "a refused read wrote an audit row")

    def test_over_http_the_active_seasons_own_grants_still_carry_venue_name(self):
        admin = self._admin_ref = self._login("admin")
        fx = self._fixture(admin)
        self._assert_distinguishable(fx)
        status, raw = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{fx['season_a']['id']}/venue-access")
        self.assertEqual(status, 200, raw[:300])
        rows = {r["venue_id"]: r for r in json.loads(raw)["venue_access"]}
        self.assertEqual(set(rows), {fx["home"]["id"], fx["retired"]["id"],
                                     fx["shared"]["id"]}, raw[:400])
        self.assertTrue(rows[fx["home"]["id"]]["active"])
        self.assertFalse(rows[fx["retired"]["id"]]["active"],
                         "the revoked grant row vanished from the listing")
        self.assertEqual(rows[fx["home"]["id"]]["venue_name"],
                         "HVA-HOME-RINK-A")
        self.assertEqual(rows[fx["retired"]["id"]]["venue_name"],
                         "HVA-RETIRED-RINK-A")
        self.assertEqual(
            rows[fx["shared"]["id"]]["venue_name"], fx["shared"]["name"],
            "a legitimately shared cross-Program arena lost its name")
        self.assertNotIn(fx["probe"]["name"].encode(), raw)
        self.assertNotIn(fx["probe"]["id"].encode(), raw)


class SelectedSeasonCeilingHttpTest(unittest.TestCase):
    """The owner ruling over the REAL transport, with a real session.

    The facade cases above hand ``role``/``scope`` in by hand and would agree
    with themselves even if the routes never threaded the authenticated
    identity through -- which is exactly what the venue-access route failed to
    do in the first place. Only here is the ruling proven at the layer an
    operator actually reaches, for BOTH routes, including the raw-body
    indistinguishability that a facade-level dict comparison cannot see.

    Transport helpers are the same shape as the two HTTP classes above; they
    are repeated rather than shared so those classes stay untouched.
    """

    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)

    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, http.client.HTTPException) as e:
            return 0, json.dumps({"transport_error": repr(e)}).encode()

    def _login(self, username, password="demo"):
        c = self._client()
        status, _ = self._req(c, "POST", "/api/auth/login",
                              {"username": username, "password": password})
        self.assertEqual(status, 200)
        return c

    def _v2(self, c, entity, body):
        status, raw = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, raw[:300])
        return json.loads(raw)

    def _grant(self, c, season_id, venue_id):
        # The grant is a WRITE naming an EXISTING Season, so the Season end is
        # ceilinged on the caller's ACTIVE Season (#369 target authorization):
        # a grant made while a different Season is selected is refused with the
        # generic "Season <id> not found.". Select the destination Season
        # first, exactly as the UI's context bar does -- the production guard
        # is honoured here, never bypassed. Each test sets the context it is
        # actually about afterwards.
        program_id = self.srv.STATE.api.store.get_season(season_id).program_id
        status, raw = self._req(c, "POST", "/api/context",
                                {"program_id": program_id,
                                 "season_id": season_id})
        self.assertEqual(status, 200, raw[:300])
        status, raw = self._req(
            c, "POST", f"/api/v2/setup/seasons/{season_id}/venue-access",
            {"venue_id": venue_id})
        self.assertEqual(status, 200, raw[:300])
        return json.loads(raw)

    def _select(self, c, program_id, season_id):
        status, raw = self._req(c, "POST", "/api/context",
                                {"program_id": program_id,
                                 "season_id": season_id})
        self.assertEqual(status, 200, raw[:300])
        return json.loads(raw)

    # The two routes the ruling names, addressed the way a client does.
    def _paths(self, season_id):
        return (("venue-access",
                 f"/api/v2/setup/seasons/{season_id}/venue-access"),
                ("candidates",
                 f"/api/v2/setup/seasons/{season_id}/venue-candidates"))

    def _fixture(self, c):
        """The HTTP mirror of ``_ceiling_fixture``: Program A with TWO Seasons,
        Program B with one, and a genuinely shared arena."""
        org_a = self._v2(c, "organization",
                         {"name": "HC Org A", "short_name": "HCA"})
        prog_a = self._v2(c, "program",
                          {"name": "HC Prog A",
                           "operator_organization_id": org_a["id"]})
        season_a = self._v2(c, "season",
                            {"program_id": prog_a["id"], "name": "HC Season A1"})
        sibling_a = self._v2(c, "season",
                             {"program_id": prog_a["id"], "name": "HC Season A2"})
        org_b = self._v2(c, "organization",
                         {"name": "HC Org B", "short_name": "HCB"})
        prog_b = self._v2(c, "program",
                          {"name": "HC Prog B",
                           "operator_organization_id": org_b["id"]})
        season_b = self._v2(c, "season",
                            {"program_id": prog_b["id"], "name": "HC Season B1"})

        home = self._v2(c, "venue", {"name": "HC-HOME-RINK-A1",
                                     "organization_id": org_a["id"]})
        self._grant(c, season_a["id"], home["id"])
        retired = self._v2(c, "venue", {"name": "HC-RETIRED-RINK-A1",
                                        "organization_id": org_a["id"]})
        gone = self._grant(c, season_a["id"], retired["id"])
        status, raw = self._req(
            c, "POST",
            f"/api/v2/setup/season-venue-access/{gone['id']}/remove", {})
        self.assertEqual(status, 200, raw[:300])
        shared = self._v2(c, "venue", {"name": f"HC-{CEIL_SHARED}",
                                       "organization_id": org_b["id"]})
        self._grant(c, season_b["id"], shared["id"])
        self._grant(c, season_a["id"], shared["id"])
        cross = self._v2(c, "venue", {"name": f"HC-{CEIL_CROSS}",
                                      "organization_id": org_b["id"]})
        self._grant(c, season_b["id"], cross["id"])
        sibling_venue = self._v2(c, "venue", {"name": f"HC-{CEIL_SIBLING}",
                                              "organization_id": org_a["id"]})
        self._grant(c, sibling_a["id"], sibling_venue["id"])
        return {"prog_a": prog_a, "season_a": season_a, "sibling_a": sibling_a,
                "prog_b": prog_b, "season_b": season_b, "home": home,
                "retired": retired, "shared": shared, "cross": cross,
                "sibling_venue": sibling_venue}

    def _assert_preconditions(self, c, fx, expect_season):
        """The same non-vacuity guarantees the facade class asserts, plus the
        one only the transport can give: that the SERVER really persisted the
        selection this case is about."""
        store = self.srv.STATE.api.store
        sel = store.get_season(fx["season_a"]["id"])
        sib = store.get_season(fx["sibling_a"]["id"])
        foreign = store.get_season(fx["season_b"]["id"])
        self.assertNotEqual(sib.id, sel.id)
        self.assertEqual(
            sib.program_id, sel.program_id,
            "the 'sibling' Season is not in the selected Season's Program, so "
            "this fixture never exercises the same-Program/other-Season case")
        self.assertNotEqual(
            foreign.program_id, sel.program_id,
            "both Seasons are in one Program: nothing cross-Program to refuse")
        grants = {k: {g.venue_id for g in
                      store.season_venue_access_for_season(sid)}
                  for k, sid in (("sel", sel.id), ("sib", sib.id),
                                 ("foreign", foreign.id))}
        self.assertIn(fx["shared"]["id"], grants["sel"])
        self.assertIn(
            fx["shared"]["id"], grants["foreign"],
            "the 'shared' arena is not actually shared with Program B, so "
            "naming it proves nothing about cross-Program sharing")
        self.assertIn(
            fx["sibling_venue"]["id"], grants["sib"],
            "the sibling Season holds no Venue of its own, so reading it "
            "could not have disclosed one anyway")
        self.assertNotIn(fx["sibling_venue"]["id"], grants["sel"])
        self.assertIn(fx["cross"]["id"], grants["foreign"])
        self.assertNotIn(fx["cross"]["id"], grants["sel"])
        names = [fx[k]["name"] for k in
                 ("home", "retired", "shared", "cross", "sibling_venue")]
        self.assertEqual(len(set(names)), len(names),
                         f"two fixture Venues share a name: {names}")
        # And the persisted selection really is what this case assumes.
        status, raw = self._req(c, "GET", "/api/context")
        self.assertEqual(status, 200, raw[:300])
        ctx = json.loads(raw)
        self.assertEqual(ctx.get("program_id"), fx["prog_a"]["id"], raw[:300])
        self.assertEqual(
            ctx.get("season_id"), expect_season,
            f"the server persisted season {ctx.get('season_id')!r}, not "
            f"{expect_season!r} -- this case would prove something else")

    def _tokens(self, fx):
        return (fx["sibling_a"]["id"], fx["sibling_a"]["name"],
                fx["season_b"]["id"], fx["season_b"]["name"],
                fx["prog_b"]["id"],
                fx["sibling_venue"]["id"], fx["sibling_venue"]["name"],
                fx["cross"]["id"], fx["cross"]["name"],
                fx["shared"]["id"], fx["shared"]["name"])

    def test_over_http_the_selected_season_still_serves_both_contracts(self):
        admin = self._login("admin")
        fx = self._fixture(admin)
        self._select(admin, fx["prog_a"]["id"], fx["season_a"]["id"])
        self._assert_preconditions(admin, fx, fx["season_a"]["id"])

        status, raw = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{fx['season_a']['id']}/venue-access")
        self.assertEqual(status, 200, raw[:300])
        rows = {r["venue_id"]: r for r in json.loads(raw)["venue_access"]}
        self.assertEqual(set(rows), {fx["home"]["id"], fx["retired"]["id"],
                                     fx["shared"]["id"]}, raw[:400])
        self.assertTrue(rows[fx["home"]["id"]]["active"])
        self.assertFalse(rows[fx["retired"]["id"]]["active"],
                         "the revoked grant row vanished from the listing")
        self.assertEqual(rows[fx["home"]["id"]]["venue_name"],
                         "HC-HOME-RINK-A1")
        self.assertEqual(rows[fx["retired"]["id"]]["venue_name"],
                         "HC-RETIRED-RINK-A1")
        self.assertEqual(
            rows[fx["shared"]["id"]]["venue_name"], fx["shared"]["name"],
            "a legitimately shared cross-Program arena lost its name")
        for token in (fx["sibling_venue"]["id"], fx["sibling_venue"]["name"],
                      fx["cross"]["id"], fx["cross"]["name"],
                      fx["sibling_a"]["id"], fx["season_b"]["id"]):
            self.assertNotIn(token.encode(), raw, token)

        status, raw = self._req(
            admin, "GET",
            f"/api/v2/setup/seasons/{fx['season_a']['id']}/venue-candidates")
        self.assertEqual(status, 200, raw[:300])
        ids = {c["id"] for c in json.loads(raw)["candidates"]}
        self.assertIn(
            fx["cross"]["id"], ids,
            "a Venue linked only to ANOTHER Program is no longer a candidate "
            "-- the destination-Season ceiling must not have narrowed the "
            "candidate SET, or arena sharing deadlocks again")
        self.assertIn(
            fx["sibling_venue"]["id"], ids,
            "a Venue established by a sibling Season's grant is not offerable")
        self.assertIn(fx["retired"]["id"], ids)
        self.assertNotIn(fx["home"]["id"], ids)
        self.assertNotIn(fx["shared"]["id"], ids)
        self.assertNotIn(fx["sibling_a"]["id"].encode(), raw)
        self.assertNotIn(fx["season_b"]["id"].encode(), raw)

    def test_over_http_a_sibling_season_answers_exactly_like_a_missing_one(self):
        admin = self._login("admin")
        fx = self._fixture(admin)
        self._select(admin, fx["prog_a"]["id"], fx["season_a"]["id"])
        self._assert_preconditions(admin, fx, fx["season_a"]["id"])
        store = self.srv.STATE.api.store
        grants_before = {g.id: g.active for g in store.all_season_venue_access()}
        audit_before = len(store.all_setup_audit())

        cases = {
            "sibling": fx["sibling_a"]["id"],      # the ruling's headline case
            "foreign": fx["season_b"]["id"],
            "missing": "season_never_existed",
        }
        for name_idx in (0, 1):
            answers = {}
            for label, sid in cases.items():
                route, path = self._paths(sid)[name_idx]
                status, body = self._req(admin, "GET", path)
                self.assertEqual(
                    status, 404,
                    f"[{route}/{label}] answered {status} while Season A1 was "
                    f"selected: {body[:300]!r}")
                answers[label] = _mask(body, sid)
            route = self._paths("x")[name_idx][0]
            self.assertEqual(
                answers["sibling"], answers["missing"],
                f"[{route}] a SIBLING Season of the ACTIVE Program is "
                f"distinguishable from a nonexistent one: "
                f"{answers['sibling']!r} vs {answers['missing']!r}")
            self.assertEqual(
                answers["foreign"], answers["missing"],
                f"[{route}] a foreign-Program Season is distinguishable from "
                f"a nonexistent one: {answers['foreign']!r} vs "
                f"{answers['missing']!r}")
            for label, raw in answers.items():
                for token in self._tokens(fx):
                    self.assertNotIn(
                        token.encode(), raw,
                        f"[{route}/{label}] the refusal disclosed {token!r}")

        self.assertEqual({g.id: g.active
                          for g in store.all_season_venue_access()},
                         grants_before, "a refused read changed grants")
        self.assertEqual(len(store.all_setup_audit()), audit_before,
                         "a refused read wrote an audit row")

    def test_over_http_a_program_only_context_fails_closed_for_both(self):
        admin = self._login("admin")
        fx = self._fixture(admin)
        # Program-only is a first-class selection (#159): no Season chosen.
        self._select(admin, fx["prog_a"]["id"], None)
        self._assert_preconditions(admin, fx, None)
        store = self.srv.STATE.api.store
        grants_before = {g.id: g.active for g in store.all_season_venue_access()}
        audit_before = len(store.all_setup_audit())

        for name_idx in (0, 1):
            route, own_path = self._paths(fx["season_a"]["id"])[name_idx]
            _r, missing_path = self._paths("season_never_existed")[name_idx]
            st_own, body_own = self._req(admin, "GET", own_path)
            st_missing, body_missing = self._req(admin, "GET", missing_path)
            self.assertEqual(
                st_own, 404,
                f"[{route}] a Season of the active Program answered {st_own} "
                f"with NO Season selected: {body_own[:300]!r}")
            self.assertEqual(st_missing, 404, body_missing[:300])
            raw_own = _mask(body_own, fx["season_a"]["id"])
            raw_missing = _mask(body_missing, "season_never_existed")
            self.assertEqual(
                raw_own, raw_missing,
                f"[{route}] failing closed is distinguishable from a "
                f"nonexistent Season: {raw_own!r} vs {raw_missing!r}")
            for token in self._tokens(fx) + (fx["home"]["id"],
                                             fx["home"]["name"]):
                self.assertNotIn(token.encode(), raw_own, f"[{route}] {token!r}")

        self.assertEqual({g.id: g.active
                          for g in store.all_season_venue_access()},
                         grants_before, "a refused read changed grants")
        self.assertEqual(len(store.all_setup_audit()), audit_before,
                         "a refused read wrote an audit row")
