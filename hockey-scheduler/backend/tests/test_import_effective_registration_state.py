"""One post-import effective-state registration_number uniqueness plan,
shared by preview and apply, plus swap/cycle-safe commit-side staging
(#273 review round 3 finding 1).

Round 2 (test_registration_number_invariant.py) closed the WRITE-time gap:
both import commits now refuse a same-team duplicate governing-body id even
when a row's registration_number cell is blank (a RETAIN, never a clear) and
the row's team_code moves the player onto a team that already holds that
retained number. It left two problems on the table, both closed here:

1. The pure PRE-COMMIT validator (``validate_import`` /
   ``validate_hierarchy_import`` — the SAME function both the dry-run
   preview and the commit's own pre-write gate call) still only checked a
   row's own literally-SUPPLIED cell, so it (a) waved a blank-cell Team move
   onto an already-occupied number through as ``ok: True`` (a false-safe
   preview — the commit then refused it, correctly, but only the write-time
   backstop caught it, not the validator both call sites share), and
   (b) flagged a valid same-team registration SWAP as a same-team duplicate,
   because two rows each supplying the OTHER's current number briefly "share
   a value" under a naive scan even though the FINAL state has no collision
   at all.
2. Even once the validator agreed a swap/cycle was clean, the COMMIT's
   sequential per-row apply could not actually LAND it: the first row's
   write collided with the number a later row in the same batch still held,
   because nothing staged the moves first — an exact analog of the #292
   jersey-swap bug, just never ported to registration_number.

``domain.identity.plan_effective_registration_state`` (a pure function) now
computes the FINAL per-team registration_number occupancy once; both
``import_validator._check_player_identity`` and
``hierarchy_import._check_player_duplicates`` feed it the row's EFFECTIVE
value (supplied, or — blank retains — the value the matching existing
player already carries) plus every untouched existing player's own value,
and report a conflict only for a slot TWO OR MORE distinct entities still
claim once the whole batch has landed. Because commit re-runs the identical
validator as its own pre-write gate, preview and commit AGREE by
construction whenever the store is untouched between the two calls — this
module asserts that agreement directly rather than assuming it.
``SetupService.release_batch_player_registrations`` (mirroring #292's
``release_batch_player_jerseys``) then stages a validated batch so the
sequential per-row apply can always actually realize it: every existing
player whose final (team, registration) differs from its current one is
released to NULL first, inside the same transaction, before any per-row
write.

Covers, on Memory/SQLite/[PostgreSQL]:

* the shared plan function in isolation (pure, no store);
* both import paths (legacy teams+players, nine-sheet hierarchy);
* blank/same/changed registration cells, alone and combined with a Team
  move;
* two-way same-team swaps, cross-team exchanges (team AND number both
  move), and a longer (3-4 entity) cycle;
* exact preview/commit verdict parity, asserted directly against the
  identical untouched store state;
* a TRUE final-state conflict (unresolvable by any staging) still refuses
  with zero writes and zero audits, whether the collision is row-vs-row or
  row-vs-untouched-existing-player.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
from hockey_scheduler.domain.identity import plan_effective_registration_state
from hockey_scheduler.store import InMemoryStore, SqlStore


def _backends():
    stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        pg = SqlStore(url)
        pg.reset_schema()
        stores.append(("postgres", pg))
    return stores


def _player_audits(store):
    return [a for a in store.all_setup_audit()
            if a.action in ("player_created", "player_updated", "player_added")]


def _players_by_code(store):
    return {p.external_ref: p for p in store.all_players()}


# =========================================================================== #
# The shared plan function, in isolation: pure, no store.                     #
# =========================================================================== #
class PlanEffectiveRegistrationStateUnitTest(unittest.TestCase):

    def test_empty_entries_no_conflicts(self):
        self.assertEqual(plan_effective_registration_state([]), {})

    def test_single_entity_no_conflict(self):
        self.assertEqual(
            plan_effective_registration_state([("row1", "t1", "REG-A")]), {})

    def test_two_entities_same_team_same_number_is_a_conflict(self):
        entries = [("row1", "t1", "REG-A"), ("row2", "t1", "REG-A")]
        self.assertEqual(plan_effective_registration_state(entries),
                         {("t1", "REG-A"): ["row1", "row2"]})

    def test_two_entities_different_teams_same_number_no_conflict(self):
        entries = [("row1", "t1", "REG-A"), ("row2", "t2", "REG-A")]
        self.assertEqual(plan_effective_registration_state(entries), {})

    def test_swap_two_entities_crossed_numbers_no_conflict(self):
        # A -> REG-B, B -> REG-A on the same team: two DIFFERENT final slots,
        # exactly one entity in each -- never a conflict, even though the
        # values crossed mid-scan.
        entries = [("A", "t1", "REG-B"), ("B", "t1", "REG-A")]
        self.assertEqual(plan_effective_registration_state(entries), {})

    def test_four_way_cycle_no_conflict(self):
        entries = [("A", "t1", "REG-B"), ("B", "t1", "REG-C"),
                   ("C", "t1", "REG-D"), ("D", "t1", "REG-A")]
        self.assertEqual(plan_effective_registration_state(entries), {})

    def test_none_registration_number_never_conflicts(self):
        entries = [("A", "t1", None), ("B", "t1", None)]
        self.assertEqual(plan_effective_registration_state(entries), {})

    def test_none_team_key_never_conflicts(self):
        entries = [("A", None, "REG-A"), ("B", None, "REG-A")]
        self.assertEqual(plan_effective_registration_state(entries), {})

    def test_three_way_conflict_lists_every_entity(self):
        entries = [("A", "t1", "REG-A"), ("B", "t1", "REG-A"),
                   ("C", "t1", "REG-A")]
        result = plan_effective_registration_state(entries)
        self.assertEqual(sorted(result[("t1", "REG-A")]), ["A", "B", "C"])

    def test_entity_is_a_pure_passthrough_never_unpacked(self):
        entries = [(("row", 1), "t1", "REG-A"),
                   (("existing", "p9"), "t1", "REG-A")]
        result = plan_effective_registration_state(entries)
        self.assertEqual(sorted(result[("t1", "REG-A")]),
                         sorted([("row", 1), ("existing", "p9")]))

    def test_unrelated_slot_on_another_team_is_independent(self):
        entries = [("A", "t1", "REG-A"), ("B", "t1", "REG-A"),
                   ("C", "t2", "REG-A")]
        result = plan_effective_registration_state(entries)
        self.assertEqual(set(result), {("t1", "REG-A")})


# =========================================================================== #
# Legacy teams+players import.                                                #
# =========================================================================== #
_LEGACY_HEADER = ("player_code,first_name,last_name,team_code,jersey_number,"
                  "position,email,registration_number\n")
_LEGACY_TEAMS_CSV = ("team_code,team_name,club_name,division_name\n"
                     "T1,Team One,,\nT2,Team Two,,\n")


def _legacy_players_csv(*rows):
    return _LEGACY_HEADER + "".join(r + "\n" for r in rows)


class LegacyImportEffectiveStateTest(unittest.TestCase):

    def _seed(self, store, *rows):
        api = ApiService(store)
        program = api.create_program("P", "US", "UTC")
        season = api.create_season(program["id"], "S")
        api.create_league(season["id"], "L")
        res = api.commit_teams_players_import(
            season["id"],
            {"teams_csv": _LEGACY_TEAMS_CSV,
             "players_csv": _legacy_players_csv(*rows)},
            actor_id="seed")
        self.assertTrue(res.get("committed"), res)
        return api, season["id"]

    def _team_id(self, store, code):
        return next(t.id for t in store.all_teams() if t.external_ref == code)

    def _assert_parity(self, api, season_id, payload, label):
        """Preview and commit, against the SAME untouched store state, must
        agree: both accept, or both refuse with the identical error list
        (#273 review round 3 finding 1 -- exact preview/commit parity)."""
        preview = api.get_import_dry_run(payload)
        res = api.commit_teams_players_import(season_id, payload, actor_id="op")
        self.assertEqual(preview["ok"], res.get("committed") is True,
                         (label, preview, res))
        if not preview["ok"]:
            self.assertEqual(res.get("errors"), preview["errors"], label)
        return preview, res

    # -- blank / same / changed cells on a Team move -------------------------

    def test_blank_cell_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "H,Holder,H,T2,,forward,,REG-1",
                    "M,Mover,M,T1,,forward,,REG-1")
                t1 = self._team_id(store, "T1")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "H,Holder,H,T2,,forward,,REG-1",
                              "M,Mover,M,T2,,forward,,")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], label)
                m = _players_by_code(store)["M"]
                self.assertEqual(m.team_id, t1, label)  # never moved
                if isinstance(store, SqlStore):
                    store.close()

    def test_same_value_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "H,Holder,H,T2,,forward,,REG-1",
                    "M,Mover,M,T1,,forward,,REG-1")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "H,Holder,H,T2,,forward,,REG-1",
                              "M,Mover,M,T2,,forward,,REG-1")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_changed_value_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "H,Holder,H,T2,,forward,,REG-1",
                    "M,Mover,M,T1,,forward,,REG-9")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "H,Holder,H,T2,,forward,,REG-1",
                              "M,Mover,M,T2,,forward,,REG-1")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_blank_cell_move_onto_free_number_succeeds_positive_control(self):
        # The fix must not over-block: a blank-cell Team move onto a team
        # with NO conflicting number previews clean and commits.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "M,Mover,M,T1,,forward,,REG-1")
                t2 = self._team_id(store, "T2")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "M,Mover,M,T2,,forward,,")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertTrue(preview["ok"], (label, preview))
                m = _players_by_code(store)["M"]
                self.assertEqual(m.team_id, t2, label)
                self.assertEqual(m.registration_number, "REG-1", label)  # retained
                if isinstance(store, SqlStore):
                    store.close()

    # -- swaps and cycles -----------------------------------------------------

    def test_two_way_same_team_swap_previews_clean_and_commits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "A,Ann,X,T1,,forward,,REG-A",
                    "B,Bob,Y,T1,,forward,,REG-B")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T1,,forward,,REG-B",
                              "B,Bob,Y,T1,,forward,,REG-A")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_swap_with_one_side_blank_retaining_still_commits(self):
        # A's row explicitly claims B's number; B's row is BLANK and simply
        # retains whatever it already has -- a valid swap from A's
        # perspective, expressed asymmetrically in the sheet.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "A,Ann,X,T1,,forward,,REG-A",
                    "B,Bob,Y,T1,,forward,,REG-B")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T1,,forward,,REG-B",
                              "B,Bob,Y,T1,,forward,,REG-A")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertTrue(preview["ok"], (label, preview))
                if isinstance(store, SqlStore):
                    store.close()

    def test_three_way_cycle_previews_clean_and_commits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "A,Ann,X,T1,,forward,,REG-A",
                    "B,Bob,Y,T1,,forward,,REG-B",
                    "C,Cid,Z,T1,,forward,,REG-C")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T1,,forward,,REG-B",
                              "B,Bob,Y,T1,,forward,,REG-C",
                              "C,Cid,Z,T1,,forward,,REG-A")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].registration_number, "REG-C", label)
                self.assertEqual(players["C"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_cross_team_exchange_team_and_number_both_move(self):
        # A (T1, REG-A) <-> B (T2, REG-B) trade BOTH team and number.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "A,Ann,X,T1,,forward,,REG-A",
                    "B,Bob,Y,T2,,forward,,REG-B")
                t1 = self._team_id(store, "T1")
                t2 = self._team_id(store, "T2")
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T2,,forward,,REG-B",
                              "B,Bob,Y,T1,,forward,,REG-A")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].team_id, t2, label)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].team_id, t1, label)
                self.assertEqual(players["B"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- true final-state conflicts: zero writes, zero audits ----------------

    def test_true_conflict_two_new_players_zero_writes_zero_audits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(store)  # no players yet
                audits_before = len(_player_audits(store))
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T1,,forward,,REG-A",
                              "B,Bob,Y,T1,,forward,,REG-A")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(store.all_players(), [], label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_true_conflict_row_vs_untouched_existing_player_zero_writes(self):
        # The holder is NOT itself part of this upload -- purely an
        # untouched existing player, exercising the "existing" branch of the
        # plan rather than row-vs-row.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "H,Holder,H,T2,,forward,,REG-1",
                    "M,Mover,M,T1,,forward,,REG-1")
                before = {p.external_ref: (p.team_id, p.registration_number)
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                # Only M's row is uploaded this time; H is untouched.
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "M,Mover,M,T2,,forward,,")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(preview["errors"][0].get("field"),
                                 "registration_number", label)
                after = {p.external_ref: (p.team_id, p.registration_number)
                        for p in store.all_players()}
                self.assertEqual(after, before, label)  # nothing moved
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_almost_swap_one_side_wrong_is_a_true_conflict(self):
        # A claims B's number; B does NOT reciprocate (keeps its own) --
        # this is NOT a swap, it is a genuine collision, and must still be
        # refused with zero writes even though a naive swap-detector might
        # be tempted to wave any two-row registration exchange through.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, season_id = self._seed(
                    store, "A,Ann,X,T1,,forward,,REG-A",
                    "B,Bob,Y,T1,,forward,,REG-B")
                before = {p.external_ref: p.registration_number
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                payload = {"teams_csv": _LEGACY_TEAMS_CSV,
                          "players_csv": _legacy_players_csv(
                              "A,Ann,X,T1,,forward,,REG-B",
                              "B,Bob,Y,T1,,forward,,REG-B")}
                preview, res = self._assert_parity(api, season_id, payload, label)
                self.assertFalse(preview["ok"], (label, preview))
                after = {p.external_ref: p.registration_number
                        for p in store.all_players()}
                self.assertEqual(after, before, label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()


# =========================================================================== #
# Nine-sheet hierarchy import.                                                #
# =========================================================================== #
_HIERARCHY_PLAYERS_HEADER = (
    "player_code,team_code,first_name,last_name,jersey_number,position,"
    "email,preferred_name,shoots,birthdate,registration_number,"
    "skill_rating\n")


def _hierarchy_players_csv(*rows):
    return _HIERARCHY_PLAYERS_HEADER + "".join(r + "\n" for r in rows)


class HierarchyImportEffectiveStateTest(unittest.TestCase):
    """LIONS and BEARS both exist via ``hierarchy_fixtures.full_payload``'s
    default ``permanent_teams_csv``."""

    def _seed(self, store, *rows):
        api = ApiService(store)
        payload = fx.full_payload(players_csv=_hierarchy_players_csv(*rows))
        res = api.commit_hierarchy_import(payload, actor_id="seed")
        self.assertTrue(res.get("committed"), res)
        return api, payload

    def _team_id(self, store, code):
        return next(t.id for t in store.all_teams() if t.external_ref == code)

    def _assert_parity(self, api, base_payload, players_csv, label):
        payload = dict(base_payload)
        payload["players_csv"] = players_csv
        preview = api.get_hierarchy_import_dry_run(payload)
        res = api.commit_hierarchy_import(payload, actor_id="op")
        self.assertEqual(preview["ok"], res.get("committed") is True,
                         (label, preview, res))
        if not preview["ok"]:
            self.assertEqual(res.get("errors"), preview["errors"], label)
        return preview, res

    # -- blank / same / changed cells on a Team move -------------------------

    def test_blank_cell_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                    "M,LIONS,Mover,M,,forward,,,,,REG-1,")
                bears = self._team_id(store, "BEARS")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                        "M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], label)
                m = _players_by_code(store)["M"]
                self.assertNotEqual(m.team_id, bears, label)  # never moved
                if isinstance(store, SqlStore):
                    store.close()

    def test_same_value_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                    "M,LIONS,Mover,M,,forward,,,,,REG-1,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                        "M,BEARS,Mover,M,,forward,,,,,REG-1,"),
                    label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_changed_value_move_onto_occupied_number_refused_both_layers(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                    "M,LIONS,Mover,M,,forward,,,,,REG-9,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                        "M,BEARS,Mover,M,,forward,,,,,REG-1,"),
                    label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_blank_cell_move_onto_free_number_succeeds_positive_control(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "M,LIONS,Mover,M,,forward,,,,,REG-1,")
                bears = self._team_id(store, "BEARS")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                m = _players_by_code(store)["M"]
                self.assertEqual(m.team_id, bears, label)
                self.assertEqual(m.registration_number, "REG-1", label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- swaps and cycles -----------------------------------------------------

    def test_two_way_same_team_swap_previews_clean_and_commits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,LIONS,Ann,X,,forward,,,,,REG-A,",
                    "B,LIONS,Bob,Y,,forward,,,,,REG-B,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,,forward,,,,,REG-B,",
                        "B,LIONS,Bob,Y,,forward,,,,,REG-A,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_three_way_cycle_previews_clean_and_commits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,LIONS,Ann,X,,forward,,,,,REG-A,",
                    "B,LIONS,Bob,Y,,forward,,,,,REG-B,",
                    "C,LIONS,Cid,Z,,forward,,,,,REG-C,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,,forward,,,,,REG-B,",
                        "B,LIONS,Bob,Y,,forward,,,,,REG-C,",
                        "C,LIONS,Cid,Z,,forward,,,,,REG-A,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].registration_number, "REG-C", label)
                self.assertEqual(players["C"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_cross_team_exchange_team_and_number_both_move(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,LIONS,Ann,X,,forward,,,,,REG-A,",
                    "B,BEARS,Bob,Y,,forward,,,,,REG-B,")
                lions = self._team_id(store, "LIONS")
                bears = self._team_id(store, "BEARS")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,BEARS,Ann,X,,forward,,,,,REG-B,",
                        "B,LIONS,Bob,Y,,forward,,,,,REG-A,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].team_id, bears, label)
                self.assertEqual(players["A"].registration_number, "REG-B", label)
                self.assertEqual(players["B"].team_id, lions, label)
                self.assertEqual(players["B"].registration_number, "REG-A", label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- true final-state conflicts: zero writes, zero audits ----------------

    def test_true_conflict_two_new_players_zero_writes_zero_audits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(store)
                audits_before = len(_player_audits(store))
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,,forward,,,,,REG-A,",
                        "B,LIONS,Bob,Y,,forward,,,,,REG-A,"),
                    label)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(store.all_players(), [], label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_true_conflict_row_vs_untouched_existing_player_zero_writes(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,,forward,,,,,REG-1,",
                    "M,LIONS,Mover,M,,forward,,,,,REG-1,")
                before = {p.external_ref: (p.team_id, p.registration_number)
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], (label, preview))
                after = {p.external_ref: (p.team_id, p.registration_number)
                        for p in store.all_players()}
                self.assertEqual(after, before, label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_almost_swap_one_side_wrong_is_a_true_conflict(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,LIONS,Ann,X,,forward,,,,,REG-A,",
                    "B,LIONS,Bob,Y,,forward,,,,,REG-B,")
                before = {p.external_ref: p.registration_number
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,,forward,,,,,REG-B,",
                        "B,LIONS,Bob,Y,,forward,,,,,REG-B,"),
                    label)
                self.assertFalse(preview["ok"], (label, preview))
                after = {p.external_ref: p.registration_number
                        for p in store.all_players()}
                self.assertEqual(after, before, label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
