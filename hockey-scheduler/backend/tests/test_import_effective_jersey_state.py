"""jersey_number joins the shared post-import effective-state uniqueness
plan, closing the hierarchy-import blank-cell destructive-clear gap (#424
round-4 external review, exact head 5a7122b).

The review's finding: a blank ``jersey_number`` cell in the NINE-SHEET
hierarchy import was applied as a destructive CLEAR rather than a RETAIN,
and could evade destination-team uniqueness in the process. On Memory and
SQLite, moving a player with a retained jersey (say ``7``) into a Team that
already had an active player wearing ``7`` previewed ``ok: True``,
``errors: []``, committed successfully, moved the player, and silently
rewrote the jersey to ``None``; a same-Team blank re-import also cleared the
stored jersey with no Team move at all.

registration_number had the identical shape of bug and was closed at #273
review round 3 finding 1 by building ONE pure post-import effective-state
uniqueness plan (``domain.identity.plan_effective_registration_state`` --
see ``test_import_effective_registration_state.py``) shared by both import
paths' previews, plus swap/cycle-safe commit-side staging
(``SetupService.release_batch_player_registrations``). This module's fix
REUSES that exact same plan function for jersey_number rather than
inventing a second, jersey-only parallel mechanism -- ``hierarchy_import.
_check_player_jersey_conflicts`` now feeds it the row's EFFECTIVE jersey
(the row's own supplied cell, or -- blank retains, never clears -- the
value the matching existing player already carries) exactly like
``_check_player_duplicates`` already does for registration_number, and
``upsert_imported_player`` computes the same effective value (from
``staged_original_jersey``, mirroring ``effective_registration``'s
``staged_original_registration``) for both the availability check and the
actual write, so a blank cell is never passed to the store as a clearing
``None`` unless nothing ever existed to retain (a brand-new player).

Legacy (two-sheet) teams+players import already retained a blank jersey
correctly (#292 review, ``commit_teams_players_import``'s own inline
release/restore) -- see ``test_jersey_swap_import.LegacyImportSwapTest`` and
``HierarchyImportSwapTest`` for that pre-existing, still-passing coverage of
EXPLICIT (non-blank) same-team swaps, a cross-team occupied exchange, and
the "Team move keeps the SAME explicit number, never falsely audited"
case. This module does not repeat that coverage; it adds what the review's
finding was actually about -- the BLANK-cell path through the NINE-SHEET
hierarchy import -- plus the swap/cycle and zero-write/zero-audit shapes
the review asked to see proven there too.

Covers, on Memory/SQLite/[PostgreSQL], nine-sheet hierarchy import only:

* the reused plan function is literally the SAME object registration_number
  uses -- not a jersey-only reimplementation;
* same-Team blank retention (no Team move at all);
* a blank-cell Team move onto a FREE number (positive control -- the fix
  must not over-block) and onto an already-OCCUPIED number (the review's
  named repro);
* explicit (non-blank) same-value and changed-value Team moves onto an
  occupied number, so the fix is not blank-cell-only;
* a three-way jersey cycle (not previously covered for jersey_number) and a
  cross-team move whose explicit claim lands exactly on the slot a
  same-batch BLANK-retained mover vacates;
* exact preview/commit verdict parity, asserted directly against the
  identical untouched store state, on every scenario above;
* a true final-state conflict (row-vs-row, and row-vs-untouched-existing-
  player) refuses with zero writes and zero audits;
* accurate ``changed_fields``: a blank-cell retain (alone, or across a Team
  move) never appears in the audit's ``changed_fields`` even when another
  field on the same row genuinely changed.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

import hierarchy_fixtures as fx
from hockey_scheduler.api import ApiService
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

def _last_update(store, code):
    updates = [a for a in store.all_setup_audit()
               if a.action == "player_updated"
               and a.detail.get("external_ref") == code]
    return updates[-1] if updates else None


def _players_by_code(store):
    return {p.external_ref: p for p in store.all_players()}


# =========================================================================== #
# Architecture: jersey_number must reuse the SAME shared plan function       #
# registration_number already uses -- not a second parallel mechanism.       #
# =========================================================================== #
class SharedPlanFunctionReuseTest(unittest.TestCase):

    def test_hierarchy_import_jersey_check_reuses_the_shared_plan_function(self):
        from hockey_scheduler.domain.identity import (
            plan_effective_registration_state)
        from hockey_scheduler.services import hierarchy_import
        self.assertIs(hierarchy_import.plan_effective_registration_state,
                     plan_effective_registration_state,
                     "hierarchy_import must call the SAME pure plan "
                     "function registration_number's SAME-team check uses "
                     "(#273 review round 3 finding 1), not a jersey-only "
                     "reimplementation (#424 round-4 review).")


# =========================================================================== #
# Nine-sheet hierarchy import: the blank-cell path the review's finding is   #
# about. Column order: player_code,team_code,first_name,last_name,          #
# jersey_number,position,email,preferred_name,shoots,birthdate,             #
# registration_number,skill_rating                                          #
# =========================================================================== #
_HIERARCHY_PLAYERS_HEADER = (
    "player_code,team_code,first_name,last_name,jersey_number,position,"
    "email,preferred_name,shoots,birthdate,registration_number,"
    "skill_rating\n")


def _hierarchy_players_csv(*rows):
    return _HIERARCHY_PLAYERS_HEADER + "".join(r + "\n" for r in rows)


class HierarchyImportJerseyEffectiveStateTest(unittest.TestCase):
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
        """Preview and commit, against the SAME untouched store state, must
        agree: both accept, or both refuse with the identical error list
        (#424 round-4 review, mirroring #273 round 3 finding 1's parity
        requirement -- now proven for jersey_number too)."""
        payload = dict(base_payload)
        payload["players_csv"] = players_csv
        preview = api.get_hierarchy_import_dry_run(payload)
        res = api.commit_hierarchy_import(payload, actor_id="op")
        self.assertEqual(preview["ok"], res.get("committed") is True,
                         (label, preview, res))
        if not preview["ok"]:
            self.assertEqual(res.get("errors"), preview["errors"], label)
        return preview, res

    # -- same-Team blank retention --------------------------------------------

    def test_same_team_blank_cell_retains_jersey(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "Z,LIONS,Zoe,Q,3,forward,,,,,,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv("Z,LIONS,Zoe,Q,,forward,,,,,,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                z = _players_by_code(store)["Z"]
                self.assertEqual(z.jersey_number, 3, label)  # retained
                if isinstance(store, SqlStore):
                    store.close()

    # -- blank-cell Team move: free vs. occupied destination ------------------

    def test_blank_cell_team_move_onto_free_number_succeeds_positive_control(self):
        # The fix must not over-block: a blank-cell Team move onto a team
        # with NO conflicting number previews clean, commits, keeps the
        # number, and the single audit names the Team change, not a jersey
        # change (#424 round-4: accurate changed_fields).
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "M,LIONS,Mover,M,7,forward,,,,,,")
                bears = self._team_id(store, "BEARS")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv("M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                m = _players_by_code(store)["M"]
                self.assertEqual(m.team_id, bears, label)
                self.assertEqual(m.jersey_number, 7, label)  # retained
                update = _last_update(store, "M")
                self.assertIsNotNone(update, label)
                changed = update.detail.get("changed_fields", [])
                self.assertIn("team_id", changed, label)
                self.assertNotIn("jersey_number", changed, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_blank_cell_team_move_onto_occupied_number_refused_both_layers(self):
        # The review's named repro: a retained jersey (blank cell) moved onto
        # a Team that already has an active player wearing it must be
        # refused by BOTH the preview and the commit, with zero writes.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,7,forward,,,,,,",
                    "M,LIONS,Mover,M,7,forward,,,,,,")
                before = {p.external_ref: (p.team_id, p.jersey_number)
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,7,forward,,,,,,",
                        "M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], label)
                self.assertTrue(
                    all(e.get("code") == "duplicate_jersey_number"
                        for e in preview["errors"]), (label, preview["errors"]))
                after = {p.external_ref: (p.team_id, p.jersey_number)
                        for p in store.all_players()}
                self.assertEqual(after, before, label)  # zero writes
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- explicit (non-blank) same/changed value onto an occupied number ------

    def test_explicit_same_value_move_onto_occupied_number_refused(self):
        # Not blank-cell-only: an EXPLICITLY re-supplied unchanged number
        # moved onto an occupied destination must also be refused.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,7,forward,,,,,,",
                    "M,LIONS,Mover,M,7,forward,,,,,,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,7,forward,,,,,,",
                        "M,BEARS,Mover,M,7,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_explicit_changed_value_move_onto_occupied_number_refused(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,7,forward,,,,,,",
                    "M,LIONS,Mover,M,9,forward,,,,,,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "H,BEARS,Holder,H,7,forward,,,,,,",
                        "M,BEARS,Mover,M,7,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- swaps and cycles -------------------------------------------------------

    def test_three_way_cycle_previews_clean_and_commits(self):
        # Not previously covered for jersey_number (test_jersey_swap_import.py
        # only has a two-way swap and a two-way cross-team exchange).
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,LIONS,Ann,X,7,forward,,,,,,",
                    "B,LIONS,Bob,Y,8,forward,,,,,,",
                    "C,LIONS,Cid,Z,9,forward,,,,,,")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,8,forward,,,,,,",
                        "B,LIONS,Bob,Y,9,forward,,,,,,",
                        "C,LIONS,Cid,Z,7,forward,,,,,,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].jersey_number, 8, label)
                self.assertEqual(players["B"].jersey_number, 9, label)
                self.assertEqual(players["C"].jersey_number, 7, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_explicit_claim_onto_slot_vacated_by_blank_retained_mover_commits(self):
        # A (BEARS#7) explicitly claims BEARS#8 -- currently held by B. B's
        # own row is BLANK and moves B to LIONS, retaining B's OWN current
        # value (8) there -- which simultaneously VACATES BEARS#8 for A. This
        # only resolves correctly if the swap-safe release staging is fed
        # B's true EFFECTIVE final slot (LIONS, 8), not a raw blank-is-None
        # "final" jersey (#424 round-4).
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "A,BEARS,Ann,X,7,forward,,,,,,",
                    "B,BEARS,Bob,Y,8,forward,,,,,,")
                lions = self._team_id(store, "LIONS")
                bears = self._team_id(store, "BEARS")
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,BEARS,Ann,X,8,forward,,,,,,",
                        "B,LIONS,Bob,Y,,forward,,,,,,"),
                    label)
                self.assertTrue(preview["ok"], (label, preview))
                players = _players_by_code(store)
                self.assertEqual(players["A"].team_id, bears, label)
                self.assertEqual(players["A"].jersey_number, 8, label)
                self.assertEqual(players["B"].team_id, lions, label)
                self.assertEqual(players["B"].jersey_number, 8, label)  # retained
                if isinstance(store, SqlStore):
                    store.close()

    # -- true final-state conflicts: zero writes, zero audits -----------------

    def test_true_conflict_two_new_players_zero_writes_zero_audits(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(store)  # no players yet
                audits_before = len(_player_audits(store))
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv(
                        "A,LIONS,Ann,X,7,forward,,,,,,",
                        "B,LIONS,Bob,Y,7,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(store.all_players(), [], label)
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    def test_true_conflict_row_vs_untouched_existing_player_zero_writes(self):
        # The holder (H) is NOT itself part of this upload -- purely an
        # untouched existing player, exercising the "existing" branch of the
        # plan rather than row-vs-row. Same fixture as the review's own named
        # repro, re-asserted here with explicit zero-write/zero-audit checks.
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "H,BEARS,Holder,H,7,forward,,,,,,",
                    "M,LIONS,Mover,M,7,forward,,,,,,")
                before = {p.external_ref: (p.team_id, p.jersey_number)
                         for p in store.all_players()}
                audits_before = len(_player_audits(store))
                # Only M's row is uploaded this time; H is untouched.
                preview, res = self._assert_parity(
                    api, seed_payload,
                    _hierarchy_players_csv("M,BEARS,Mover,M,,forward,,,,,,"),
                    label)
                self.assertFalse(preview["ok"], (label, preview))
                self.assertEqual(preview["errors"][0].get("field"),
                                 "jersey_number", label)
                after = {p.external_ref: (p.team_id, p.jersey_number)
                        for p in store.all_players()}
                self.assertEqual(after, before, label)  # nothing moved
                self.assertEqual(len(_player_audits(store)), audits_before, label)
                if isinstance(store, SqlStore):
                    store.close()

    # -- accurate changed_fields -----------------------------------------------

    def test_blank_jersey_retain_excluded_from_changed_fields(self):
        # A blank-cell retain must never appear in changed_fields, even when
        # another field on the SAME row genuinely changes (#424 round-4 --
        # this is exactly the shape upsert_imported_player's restore-before-
        # diff must get right: the retained value diffs against itself).
        for label, store in _backends():
            with self.subTest(backend=label):
                api, seed_payload = self._seed(
                    store, "Z,LIONS,Zoe,Q,3,forward,,,,,REG-OLD,")
                payload = dict(seed_payload)
                payload["players_csv"] = _hierarchy_players_csv(
                    "Z,LIONS,Zoe,Q,,forward,,,,,REG-NEW,")
                res = api.commit_hierarchy_import(payload, actor_id="op")
                self.assertTrue(res["committed"], (label, res))
                update = _last_update(store, "Z")
                self.assertIsNotNone(update, label)
                changed = update.detail.get("changed_fields", [])
                self.assertIn("registration_number", changed, label)
                self.assertNotIn("jersey_number", changed, label)
                z = _players_by_code(store)["Z"]
                self.assertEqual(z.jersey_number, 3, label)  # retained
                self.assertEqual(z.registration_number, "REG-NEW", label)
                if isinstance(store, SqlStore):
                    store.close()


if __name__ == "__main__":
    unittest.main()
