"""The setup hierarchy reports each Season's read-only fact (#159 follow-up).

WHY THIS EXISTS, concretely. ``get_venue_grant_candidates`` refuses an ARCHIVED
destination Season (#369 owner ruling): the candidate list exists only to feed
``grant_season_venue_access``, which ``require_active_season`` refuses on an
archived Season anyway, so offering candidates advertised an impossible mutation
-- and did it with the one facility list that deliberately reaches ACROSS the
Program ceiling. That refusal is correct and is NOT what this file questions.

What was missing is the client's ability to OBEY it. ``app.js`` gated the
candidate fetch on ``/api/context/options``' ``selected.read_only``, which is a
CLIENT-SIDE CACHE refreshed only when the options are re-fetched. Archive the
SELECTED Season and the cache still says writable until something happens to
reload it, so the next render issued the request and collected the server's
deliberate 404 -- an undeclared failed request on a surface that had already
decided not to want the data. It reproduced roughly one run in three in the
``setup-state-matrix`` browser journey, on ``.../season_3/venue-candidates``.

"Refresh the cache harder" would have left the defect CLASS alive, dependent on
every future mutation remembering to invalidate. Instead the hierarchy -- the
tree the client is already looping over in that same render pass -- carries the
fact per Season, so the guard's input and the refusal's decision come from one
read of one store -- as fresh as a client-side value can be.

THAT IS NOT THE SAME AS CURRENT, and this file does not claim it is. The
hierarchy read and the candidate request are two separate reads and CAN describe
different moments: a Season archived between them still takes the deliberate
404. Closing that window needs a server-side binding (a version/epoch on the
follow-up read), which is deliberately NOT in this change.

These tests are therefore about AGREEMENT, not about a field existing. The
field is asserted to track the refusal at every step (active -> archived ->
reopened), per Season rather than per payload, and to be derived from the WRITE
predicate rather than the broader historicity one. Every proof runs on Memory,
SQLite and PostgreSQL: ``read_only`` is a status comparison, and status
round-trips through three different persistence layers.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = ("admin", Role.LEAGUE_ADMIN, {})

# The season node's key set BEFORE this change, measured on origin/main. The
# assertions below pin it as a SUBSET that must survive, so this file proves the
# addition is additive rather than merely claiming it.
LEGACY_SEASON_NODE_KEYS = {"id", "leagues", "name", "needs_assignment"}


def _backends():
    """Memory, SQLite and -- whenever it is configured -- PostgreSQL.

    Mirrors the house pattern (test_hierarchy_program_scope.py). The
    ``_assert_all_backends_ran`` check below is what stops a silently absent
    PostgreSQL from turning a two-backend run into a green three-backend claim.
    """
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


class _HierarchyReadOnlyBase(unittest.TestCase):

    def _assert_all_backends_ran(self, ran):
        """A backend that quietly did not run is not a backend that passed."""
        self.assertIn("memory", ran, ran)
        self.assertIn("sqlite", ran, ran)
        if os.environ.get("TEST_DATABASE_URL"):
            self.assertIn(
                "postgres", ran,
                "TEST_DATABASE_URL is configured but the PostgreSQL backend "
                f"never ran; only {sorted(ran)} were exercised")

    def _season_node(self, api, program_id, season_id, backend):
        """The one Season's node out of the SCOPED hierarchy read.

        Scoped (identity passed) deliberately: that is the form the HTTP route
        serves and the form app.js renders from. The identity-less internal form
        is a different contract and is covered separately below.
        """
        tree = api.get_setup_hierarchy_v2(*ADMIN)
        self.assertNotIn("error", tree, f"[{backend}] {tree}")
        progs = [p for p in tree["programs"] if p["id"] == program_id]
        self.assertEqual(len(progs), 1, f"[{backend}] {tree}")
        nodes = [s for s in progs[0]["seasons"] if s["id"] == season_id]
        self.assertEqual(len(nodes), 1, f"[{backend}] {progs[0]}")
        return nodes[0]

    def _candidates_refused(self, api, season_id, backend):
        """Did ``get_venue_grant_candidates`` REFUSE this Season?

        The refusal is deliberately generic (``NotFoundError`` for archived,
        foreign, sibling and nonexistent alike), so this asks the question the
        client actually faces -- "did I get the list or a 404" -- rather than
        inspecting a reason the payload does not carry.
        """
        out = api.get_venue_grant_candidates(season_id, *ADMIN)
        if "error" in out:
            self.assertEqual(out["error"]["code"], "not_found",
                             f"[{backend}] {out}")
            return True
        self.assertIn("candidates", out, f"[{backend}] {out}")
        return False

    def _fixture(self, api, *, end_date=None):
        """One Program, one Season SELECTED, one League, one own Venue.

        The Venue is created by the same actor, which is what makes it a
        candidate at all (an unlinked draft is a candidate only for its own
        creator) -- so a 200 here carries a real non-empty list rather than
        being vacuously satisfied by an empty one.
        """
        program = api.create_program("RO Program", "US", "UTC")
        season = api.create_season(program["id"], "RO Season",
                                   end_date=end_date, actor_id="admin")
        api.create_league(season["id"], "RO League", actor_id="admin")
        api.set_active_context(*ADMIN, program["id"], season["id"])
        api.create_venue("RO Venue", actor_id="admin")
        return program, season


class HierarchySeasonReadOnlyTest(_HierarchyReadOnlyBase):
    """The field itself: present, additive, and tracking the lifecycle."""

    def test_archiving_the_selected_season_reports_read_only(self):
        """THE REGRESSION. Archive the SELECTED Season and the very next
        hierarchy read -- the same read the client's render pass makes -- must
        say so, with no context re-selection, no options re-fetch and nothing
        else invalidated in between. That "in between" is the whole point: it is
        exactly the window in which the client used to consult a stale cache."""
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api)

                node = self._season_node(api, program["id"], season["id"],
                                         backend)
                self.assertIn(
                    "read_only", node,
                    f"[{backend}] the hierarchy's Season node carries no "
                    f"read_only fact, so the client has nothing same-pass to "
                    f"guard on: {sorted(node)}")
                self.assertIs(
                    node["read_only"], False,
                    f"[{backend}] an ACTIVE selected Season must not be "
                    f"reported read-only")

                archived = api.archive_season(season["id"], reason="done",
                                              actor_id="admin")
                self.assertNotIn("error", archived, f"[{backend}] {archived}")

                node = self._season_node(api, program["id"], season["id"],
                                         backend)
                self.assertIs(
                    node["read_only"], True,
                    f"[{backend}] the SELECTED Season was archived and the "
                    f"hierarchy still reports it writable -- a client guarding "
                    f"on this payload would issue the grant-candidate read the "
                    f"server now refuses")
                _close(store)
        self._assert_all_backends_ran(ran)

    def test_reopening_restores_it(self):
        """It TRACKS the lifecycle rather than latching. A field that only ever
        went true would pass the archive assertion above and permanently
        suppress the picker on a reopened Season."""
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api)
                api.archive_season(season["id"], reason="done",
                                   actor_id="admin")
                self.assertIs(
                    self._season_node(api, program["id"], season["id"],
                                      backend)["read_only"], True, backend)

                reopened = api.reopen_season(season["id"], reason="ran long",
                                             actor_id="admin")
                self.assertNotIn("error", reopened, f"[{backend}] {reopened}")
                self.assertIs(
                    self._season_node(api, program["id"], season["id"],
                                      backend)["read_only"], False,
                    f"[{backend}] a REOPENED Season is writable again, so the "
                    f"hierarchy must stop reporting it read-only")
                _close(store)
        self._assert_all_backends_ran(ran)

    def test_the_addition_is_additive(self):
        """Every key the season node carried on origin/main is still there,
        unchanged, and exactly one key was added."""
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api)
                node = self._season_node(api, program["id"], season["id"],
                                         backend)
                missing = LEGACY_SEASON_NODE_KEYS - set(node)
                self.assertEqual(
                    missing, set(),
                    f"[{backend}] the Season node LOST pre-existing keys "
                    f"{sorted(missing)}; this change is additive only")
                self.assertEqual(
                    set(node), LEGACY_SEASON_NODE_KEYS | {"read_only"},
                    f"[{backend}] the Season node's key set is "
                    f"{sorted(node)}; exactly one key (read_only) may be added")
                # The pre-existing keys still carry their own meanings -- an
                # addition that hollowed them out would satisfy a key-set check.
                self.assertEqual(node["id"], season["id"], backend)
                self.assertEqual(node["name"], "RO Season", backend)
                self.assertEqual([lg["name"] for lg in node["leagues"]],
                                 ["RO League"], backend)
                self.assertEqual(node["needs_assignment"]["registrations"], [],
                                 backend)
                _close(store)
        self._assert_all_backends_ran(ran)

    def test_read_only_is_per_season_not_per_selection(self):
        """The fact belongs to the ROW, not to the payload.

        Two Seasons in one Program, ONE archived. A field derived from the
        caller's active Season -- an easy way to make the archive test above
        pass -- would mark both, or neither, and would silently suppress the
        picker on the writable sibling the operator switches to."""
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, archived_season = self._fixture(api)
                live = api.create_season(program["id"], "RO Season Live",
                                         actor_id="admin")
                api.archive_season(archived_season["id"], reason="done",
                                   actor_id="admin")

                # Selected = the ARCHIVED one.
                api.set_active_context(*ADMIN, program["id"],
                                       archived_season["id"])
                self.assertIs(
                    self._season_node(api, program["id"],
                                      archived_season["id"],
                                      backend)["read_only"], True, backend)
                self.assertIs(
                    self._season_node(api, program["id"], live["id"],
                                      backend)["read_only"], False,
                    f"[{backend}] a writable sibling Season is reported "
                    f"read-only merely because the SELECTED Season is archived")

                # Selected = the LIVE one; the archived sibling keeps its fact.
                api.set_active_context(*ADMIN, program["id"], live["id"])
                self.assertIs(
                    self._season_node(api, program["id"],
                                      archived_season["id"],
                                      backend)["read_only"], True,
                    f"[{backend}] the archived Season stopped reporting "
                    f"read-only once it was no longer the selection")
                self.assertIs(
                    self._season_node(api, program["id"], live["id"],
                                      backend)["read_only"], False, backend)
                _close(store)
        self._assert_all_backends_ran(ran)

    def test_the_unscoped_internal_form_carries_it_too(self):
        """The identity-less form several internal callers use gains the field
        as well, so the two forms cannot describe the same Season differently."""
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api)
                api.archive_season(season["id"], reason="done",
                                   actor_id="admin")
                tree = api.get_setup_hierarchy_v2()
                node = next(s for p in tree["programs"]
                            for s in p["seasons"] if s["id"] == season["id"])
                self.assertIs(node["read_only"], True, backend)
                _close(store)
        self._assert_all_backends_ran(ran)


class HierarchyReadOnlyAgreesWithTheRefusalTest(_HierarchyReadOnlyBase):
    """The property that actually matters: the advertised fact and the enforced
    refusal are the same fact."""

    def test_read_only_matches_the_candidate_refusal_at_every_step(self):
        """For the SELECTED Season, hierarchy ``read_only`` is true EXACTLY when
        ``get_venue_grant_candidates`` refuses.

        This is the assertion a field that merely exists cannot satisfy: a
        constant, a copy of the wrong predicate, or a field reporting some other
        Season's status all break it at one of the three steps.
        """
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api)

                for step, act in (
                        ("active", None),
                        ("archived", lambda: api.archive_season(
                            season["id"], reason="done", actor_id="admin")),
                        ("reopened", lambda: api.reopen_season(
                            season["id"], reason="ran long",
                            actor_id="admin"))):
                    if act is not None:
                        out = act()
                        self.assertNotIn("error", out, f"[{backend}] {out}")
                    node = self._season_node(api, program["id"], season["id"],
                                             backend)
                    refused = self._candidates_refused(api, season["id"],
                                                       backend)
                    self.assertIs(
                        node["read_only"], refused,
                        f"[{backend}/{step}] the hierarchy says "
                        f"read_only={node['read_only']} while the "
                        f"grant-candidate read "
                        f"{'REFUSES' if refused else 'ANSWERS'} -- the client "
                        f"guard and the server refusal disagree, which is "
                        f"precisely the 404 this field exists to prevent")
                    # ...and when it answers, it answers with real content, so
                    # "they agree" is never satisfied by an empty 200.
                    if not refused:
                        out = api.get_venue_grant_candidates(season["id"],
                                                             *ADMIN)
                        self.assertEqual(
                            [v["name"] for v in out["candidates"]],
                            ["RO Venue"], f"[{backend}/{step}] {out}")
                _close(store)
        self._assert_all_backends_ran(ran)

    def test_an_elapsed_end_date_alone_is_not_read_only(self):
        """THE FALSIFIER for picking the wrong predicate.

        ``season_guard`` holds TWO notions: ``season_is_historical`` (ARCHIVED
        *or* a definitely-elapsed ``end_date``) and ``season_is_read_only``
        (ARCHIVED, full stop). Only the latter is what any write -- or the
        candidate read -- refuses on. Deriving the DTO from historicity would
        pass every other test in this file and then silently withhold the Allow
        picker from every Season whose end_date has passed but which is still
        perfectly writable.
        """
        ran = set()
        for backend, store in _backends():
            with self.subTest(backend=backend):
                ran.add(backend)
                api = ApiService(store)
                program, season = self._fixture(api, end_date="2000-01-31")

                node = self._season_node(api, program["id"], season["id"],
                                         backend)
                self.assertIs(
                    node["read_only"], False,
                    f"[{backend}] a long-past but NEVER-ARCHIVED Season is "
                    f"reported read-only; read_only is the WRITE rule "
                    f"(archived), not the historicity rule (archived or dated)")
                self.assertFalse(
                    self._candidates_refused(api, season["id"], backend),
                    f"[{backend}] the server itself still answers for this "
                    f"Season, so a client that skipped the read on it would "
                    f"withhold a picker that works")
                _close(store)
        self._assert_all_backends_ran(ran)


if __name__ == "__main__":
    unittest.main()
