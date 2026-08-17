"""New-Season copy-forward preview/commit (#159).

"A new Season can be previewed/copied forward without mutating the prior
Season." Composes ``create_season`` with ``roll_forward_registrations_v2``'s
own selection machinery, modeled on ``preview_ice_availability``/
``commit_ice_availability``'s fingerprint pattern (#158): preview is
side-effect-free (plus an optional audit row on success), and commit refuses
before any write unless the caller's ``copy_forward_fingerprint`` (a) is
supplied, (b) matches a FRESH re-resolution of the plan against current
state, and (c) matches a successful preview audit for the SAME actor.

Three test classes:

* ``NewSeasonCopyForwardServiceTest`` -- Memory-backend coverage of every
  validation shape (cross-program refusal, wrong-league, duplicate
  selection, the division-carry-forward contract, all-or-nothing on any
  invalid selection).
* ``NewSeasonCopyForwardSqlTest`` -- runs on SQLite always and on
  PostgreSQL when ``TEST_DATABASE_URL`` is set (CI), proving over a REAL
  transactional store that preview writes nothing, a refused commit writes
  nothing, and a successful commit creates exactly the right Season +
  registrations atomically.
* ``NewSeasonCopyForwardHttpTest`` -- the two new routes over real HTTP:
  authorized commit, unauthorized (403) refusal, and malformed-body 400s.

This file adds ONLY new tests. ``roll_forward_registrations_v2``'s own
pre-existing suite (test_season_rollover.py, test_exact_registration_
identity.py, test_history_preserved.py, test_v2_canonical_invariants.py,
test_v2_reassignment_integrity_sql.py) is untouched and must stay green,
proving the extracted ``_apply_registration_selections`` helper changed
nothing about the standalone rollover's own behavior.
"""

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
from hockey_scheduler.web.server import STATE, Handler

ADMIN = "setup_admin"


def _sql_backends():
    backends = [("sqlite", ":memory:")]
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        backends.append(("postgres", url))
    return backends


def _fresh(url):
    store = SqlStore(url)
    if url != ":memory:":
        store.reset_schema()
    return store


def _teardown(store):
    try:
        if store.backend == "postgres":
            store.reset_schema()
    finally:
        store.close()


class _Fixture:
    """Program + source Season with one competition League, one Division,
    one permanent Team actively registered in the source Season -- the
    minimal hierarchy every test in this file previews/commits against."""

    def __init__(self, api, tag=""):
        self.api = api
        self.program = api.create_program(f"P{tag}", actor_id=ADMIN)
        self.src = api.create_season(
            self.program["id"], f"2025-26{tag}", actor_id=ADMIN)
        self.league = api.create_league(
            self.src["id"], f"Comp{tag}", actor_id=ADMIN)
        self.div = api.create_division(
            self.src["id"], f"Div{tag}", league_id=self.league["id"],
            actor_id=ADMIN)
        club = api.create_club(f"C{tag}", actor_id=ADMIN)
        self.team = api.create_team(
            club["id"], self.div["id"], f"Lions{tag}", actor_id=ADMIN)
        self.reg = api.register_team_for_season(
            self.src["id"], self.team["id"], self.div["id"], actor_id=ADMIN)

    def selection(self, **overrides):
        sel = {"team_id": self.team["id"], "league_id": self.league["id"]}
        sel.update(overrides)
        return sel


class NewSeasonCopyForwardServiceTest(unittest.TestCase):
    """Memory-backend coverage of the full validation contract."""

    def setUp(self):
        self.api = ApiService(InMemoryStore())
        self.fx = _Fixture(self.api)

    # -- helpers -------------------------------------------------------
    def _preview(self, **kw):
        kw.setdefault("program_id", self.fx.program["id"])
        kw.setdefault("name", "2026-27")
        kw.setdefault("source_season_id", self.fx.src["id"])
        kw.setdefault("selections", [self.fx.selection()])
        kw.setdefault("actor_id", ADMIN)
        return self.api.preview_new_season_copy_forward(**kw)

    def _commit(self, fingerprint, **kw):
        kw.setdefault("program_id", self.fx.program["id"])
        kw.setdefault("name", "2026-27")
        kw.setdefault("source_season_id", self.fx.src["id"])
        kw.setdefault("selections", [self.fx.selection()])
        kw.setdefault("actor_id", ADMIN)
        return self.api.commit_new_season_copy_forward(
            copy_forward_fingerprint=fingerprint, **kw)

    def _season_ids(self):
        return {s.id for s in self.api.store.all_seasons()}

    def _audit_count(self):
        return len(self.api.store.all_setup_audit())

    # -- preview ---------------------------------------------------------
    def test_preview_returns_fingerprint_and_reviewed_plan(self):
        resp = self._preview()
        self.assertIn("copy_forward_fingerprint", resp)
        self.assertTrue(resp["copy_forward_fingerprint"])
        self.assertEqual(resp["program_id"], self.fx.program["id"])
        self.assertEqual(resp["source_season_id"], self.fx.src["id"])
        self.assertEqual(resp["name"], "2026-27")
        self.assertEqual(len(resp["selections"]), 1)
        row = resp["selections"][0]
        self.assertEqual(row["team_id"], self.fx.team["id"])
        self.assertEqual(row["league_id"], self.fx.league["id"])
        self.assertFalse(row["division_pending"])
        self.assertIsNone(row["source_division_id"])
        self.assertEqual(resp["totals"],
                         {"teams": 1, "leagues": 1, "divisions_pending": 0})

    def test_preview_is_side_effect_free_aside_from_its_own_audit(self):
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        self._preview()
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits + 1)
        entry = self.api.store.all_setup_audit()[-1]
        self.assertEqual(entry.action, "new_season_copy_forward_previewed")
        self.assertEqual(entry.actor_id, ADMIN)

    def test_preview_with_no_actor_writes_no_audit(self):
        before_audits = self._audit_count()
        resp = self._preview(actor_id=None)
        self.assertIn("copy_forward_fingerprint", resp)
        self.assertEqual(self._audit_count(), before_audits)

    def test_preview_deterministic_same_inputs_same_fingerprint(self):
        f1 = self._preview()["copy_forward_fingerprint"]
        f2 = self._preview()["copy_forward_fingerprint"]
        self.assertEqual(f1, f2)

    def test_preview_with_source_division_marks_pending(self):
        resp = self._preview(
            selections=[self.fx.selection(division_id=self.fx.div["id"])])
        row = resp["selections"][0]
        self.assertTrue(row["division_pending"])
        self.assertEqual(row["source_division_id"], self.fx.div["id"])
        self.assertEqual(row["source_division_name"], self.fx.div["name"])
        self.assertEqual(resp["totals"]["divisions_pending"], 1)

    def test_division_not_in_source_league_season_is_rejected(self):
        other_season = self.api.create_season(
            self.fx.program["id"], "Other", actor_id=ADMIN)
        other_league = self.api.create_league(
            other_season["id"], "OtherComp", actor_id=ADMIN)
        stray_div = self.api.create_division(
            other_season["id"], "StrayDiv", league_id=other_league["id"],
            actor_id=ADMIN)
        before_audits = self._audit_count()
        res = self._preview(
            selections=[self.fx.selection(division_id=stray_div["id"])])
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"],
                         "division_needs_target_creation")
        self.assertEqual(self._audit_count(), before_audits)

    # -- commit refusals (all zero-write) ---------------------------------
    def test_commit_without_fingerprint_is_refused(self):
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        res = self._commit(None)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits)

    def test_commit_with_fabricated_fingerprint_is_refused(self):
        self._preview()
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        res = self._commit("not-a-real-fingerprint")
        self.assertEqual(res["error"]["code"], "schedule_conflict")
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits)

    def test_commit_with_never_previewed_fingerprint_is_refused(self):
        # A fingerprint that DOES match a freshly-resolved plan (so
        # preview_mismatch cannot fire) but was never actually previewed by
        # anyone (actor_id=None records no audit) -- the audit-binding half
        # of the gate, isolated from the fingerprint-match half.
        never_previewed = self.api.preview_new_season_copy_forward(
            program_id=self.fx.program["id"], name="Never Previewed",
            source_season_id=self.fx.src["id"],
            selections=[self.fx.selection()], actor_id=None)
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        res = self._commit(never_previewed["copy_forward_fingerprint"],
                           name="Never Previewed")
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits)

    def test_commit_with_a_different_actors_fingerprint_is_refused(self):
        fp = self._preview(actor_id="someone_else")["copy_forward_fingerprint"]
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        res = self._commit(fp, actor_id=ADMIN)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"], "preview_required")
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits)

    def test_commit_with_stale_fingerprint_after_the_form_changed_is_refused(self):
        fp = self._preview(name="Original Name")["copy_forward_fingerprint"]
        before_seasons = self._season_ids()
        before_audits = self._audit_count()
        res = self._commit(fp, name="Edited Name")
        self.assertEqual(res["error"]["code"], "schedule_conflict")
        self.assertEqual(res["error"]["details"]["reason"], "preview_mismatch")
        self.assertEqual(self._season_ids(), before_seasons)
        self.assertEqual(self._audit_count(), before_audits)

    def test_commit_with_stale_fingerprint_after_source_registration_changed(self):
        fp = self._preview()["copy_forward_fingerprint"]
        # The source Season's participation changes between preview and
        # commit: the team's only source registration is deactivated.
        reg = self.api.store.get_season_team_registration(self.fx.reg["id"])
        reg.active = False
        self.api.store.save_season_team_registration(reg)
        before_seasons = self._season_ids()
        res = self._commit(fp)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self._season_ids(), before_seasons)

    # -- selection validation, all-or-nothing ------------------------------
    def test_cross_program_source_season_is_rejected(self):
        other = self.api.create_program("Other", actor_id=ADMIN)
        res = self._preview(program_id=other["id"])
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"],
                         "cross_program_source_season")

    def test_empty_selections_is_rejected(self):
        res = self._preview(selections=[])
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_none_selections_is_rejected(self):
        res = self._preview(selections=None)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_team_not_registered_in_source_is_rejected(self):
        stray = self.api.create_team(
            self.api.create_club("C2", actor_id=ADMIN)["id"], self.fx.div["id"],
            "Stray", actor_id=ADMIN)
        res = self._preview(selections=[
            {"team_id": stray["id"], "league_id": self.fx.league["id"]}])
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_team_selected_into_non_permanent_league_is_rejected(self):
        other_league = self.api.create_league(
            self.fx.src["id"], "OtherLeague", actor_id=ADMIN)
        res = self._preview(selections=[
            {"team_id": self.fx.team["id"], "league_id": other_league["id"]}])
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"],
                         "rollover_league_not_team_league")

    def test_duplicate_team_selection_in_one_batch_is_rejected(self):
        res = self._preview(selections=[self.fx.selection(),
                                        self.fx.selection()])
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(res["error"]["details"]["reason"],
                         "rollover_duplicate_team_selection")

    def test_malformed_selections_return_structured_validation_errors(self):
        malformed = [
            "not-a-list", {"team_id": self.fx.team["id"]}, ["just-a-string"],
            [None], [{"league_id": self.fx.league["id"]}],
            [{"team_id": ""}], [{"team_id": None}],
            [{"team_id": self.fx.team["id"]}],  # missing league_id
        ]
        for bad in malformed:
            res = self._preview(selections=bad)
            self.assertEqual(res["error"]["code"], "validation_error",
                             f"selections={bad!r} -> {res}")

    def test_missing_program_id_is_a_structured_error_not_a_500(self):
        res = self._preview(program_id=None)
        self.assertIn("error", res)

    def test_missing_source_season_id_is_a_structured_error(self):
        res = self._preview(source_season_id=None)
        self.assertEqual(res["error"]["code"], "validation_error")

    def test_nonexistent_source_season_is_not_found(self):
        res = self._preview(source_season_id="season_does_not_exist")
        self.assertEqual(res["error"]["code"], "not_found")

    # -- successful commit --------------------------------------------------
    def test_commit_success_creates_season_and_registration(self):
        fp = self._preview()["copy_forward_fingerprint"]
        before = self._season_ids()
        res = self._commit(fp)
        self.assertNotIn("error", res, res)
        self.assertTrue(res["committed"])
        new_season = res["season"]
        self.assertNotEqual(new_season["id"], self.fx.src["id"])
        self.assertEqual(new_season["program_id"], self.fx.program["id"])
        self.assertEqual(new_season["name"], "2026-27")
        self.assertEqual(self._season_ids(), before | {new_season["id"]})
        self.assertEqual(res["totals"]["rolled_forward"], 1)
        self.assertEqual(res["totals"]["skipped"], 0)
        self.assertEqual(len(res["registrations"]), 1)
        new_reg = res["registrations"][0]
        self.assertEqual(new_reg["team_id"], self.fx.team["id"])
        self.assertEqual(new_reg["season_id"], new_season["id"])
        self.assertEqual(new_reg["league_id"], self.fx.league["id"])
        self.assertIsNone(new_reg["division_id"])
        # The SOURCE season's own registration is untouched.
        src_active = [r for r in self.api.store.registrations_for_season(
            self.fx.src["id"]) if r.active]
        self.assertEqual(len(src_active), 1)
        self.assertEqual(src_active[0].id, self.fx.reg["id"])

    def test_second_commit_reusing_the_same_fingerprint_is_idempotent(self):
        """Memory-backend sequential-replay coverage (#159 review round 2)
        of the same guarantee ``NewSeasonCopyForwardSqlTest`` proves over a
        real transactional store: replaying the SAME, still-valid
        fingerprint a second time returns the FIRST commit's exact
        season/registrations/totals rather than minting a second Season."""
        fp = self._preview()["copy_forward_fingerprint"]
        before_seasons = self._season_ids()
        first = self._commit(fp)
        self.assertNotIn("error", first, first)
        after_first_seasons = self._season_ids()
        self.assertEqual(after_first_seasons,
                         before_seasons | {first["season"]["id"]})
        after_first_audits = self._audit_count()
        second = self._commit(fp)
        self.assertNotIn("error", second, second)
        self.assertEqual(second["season"]["id"], first["season"]["id"])
        self.assertEqual(second["registrations"], first["registrations"])
        self.assertEqual(second["totals"], first["totals"])
        # No second Season, no new audit rows from the losing attempt.
        self.assertEqual(self._season_ids(), after_first_seasons)
        self.assertEqual(self._audit_count(), after_first_audits)
        new_regs = self.api.store.registrations_for_season(
            first["season"]["id"])
        self.assertEqual(len(new_regs), 1)

    def test_third_commit_reusing_the_same_fingerprint_is_also_idempotent(self):
        """Idempotency holds for an arbitrary NUMBER of replays, not just a
        single retry -- a third (or Nth) resubmission still returns the
        SAME original result (#159 review round 2)."""
        before_seasons = self._season_ids()
        fp = self._preview()["copy_forward_fingerprint"]
        first = self._commit(fp)
        self.assertNotIn("error", first, first)
        self._commit(fp)
        third = self._commit(fp)
        self.assertNotIn("error", third, third)
        self.assertEqual(third["season"]["id"], first["season"]["id"])
        self.assertEqual(self._season_ids(),
                         before_seasons | {first["season"]["id"]})
        new_regs = self.api.store.registrations_for_season(
            first["season"]["id"])
        self.assertEqual(len(new_regs), 1)

    def test_commit_success_with_source_division_lands_division_less(self):
        fp = self._preview(selections=[
            self.fx.selection(division_id=self.fx.div["id"])]
        )["copy_forward_fingerprint"]
        res = self._commit(fp, selections=[
            self.fx.selection(division_id=self.fx.div["id"])])
        self.assertNotIn("error", res, res)
        self.assertIsNone(res["registrations"][0]["division_id"])

    def test_commit_is_audited(self):
        fp = self._preview()["copy_forward_fingerprint"]
        res = self._commit(fp)
        actions = [a.action for a in self.api.store.all_setup_audit()]
        self.assertIn("season_created", actions)
        self.assertIn("season_team_registered", actions)
        self.assertIn("season_registrations_rolled_forward", actions)
        self.assertIn("new_season_copy_forward_committed", actions)
        commit_audit = next(a for a in self.api.store.all_setup_audit()
                            if a.action == "new_season_copy_forward_committed")
        self.assertEqual(commit_audit.entity_id, res["season"]["id"])
        self.assertEqual(commit_audit.detail["source_season_id"],
                         self.fx.src["id"])
        self.assertEqual(commit_audit.detail["rolled_forward"], 1)

    def test_commit_all_or_nothing_when_one_of_two_selections_is_invalid(self):
        second = self.api.create_team(
            self.api.create_club("C3", actor_id=ADMIN)["id"], self.fx.div["id"],
            "Bears", actor_id=ADMIN)
        # Second team is NOT registered in the source season -- the whole
        # batch (including the perfectly valid first selection) must refuse.
        selections = [self.fx.selection(),
                     {"team_id": second["id"], "league_id": self.fx.league["id"]}]
        before = self._season_ids()
        before_audits = self._audit_count()
        res = self._preview(selections=selections)
        self.assertEqual(res["error"]["code"], "validation_error")
        self.assertEqual(self._season_ids(), before)
        self.assertEqual(self._audit_count(), before_audits)

    def test_new_season_can_carry_two_teams_two_leagues(self):
        club2 = self.api.create_club("C2", actor_id=ADMIN)
        league2 = self.api.create_league(
            self.fx.src["id"], "Comp2", actor_id=ADMIN)
        div2 = self.api.create_division(
            self.fx.src["id"], "Div2", league_id=league2["id"], actor_id=ADMIN)
        team2 = self.api.create_team(club2["id"], div2["id"], "Bears",
                                     actor_id=ADMIN)
        self.api.register_team_for_season(
            self.fx.src["id"], team2["id"], div2["id"], actor_id=ADMIN)
        selections = [self.fx.selection(),
                     {"team_id": team2["id"], "league_id": league2["id"]}]
        fp = self._preview(selections=selections)["copy_forward_fingerprint"]
        res = self._commit(fp, selections=selections)
        self.assertNotIn("error", res, res)
        self.assertEqual(res["totals"]["rolled_forward"], 2)
        self.assertEqual(
            {r["league_id"] for r in res["registrations"]},
            {self.fx.league["id"], league2["id"]})

    # -- #159 review round 3 (owner P1): replay is immune to LATER mutation
    # of the source Season / target registrations, and a malformed
    # fingerprint never reaches a store lookup ----------------------------
    def test_replay_survives_source_team_unregistered_after_commit(self):
        """Reproduces the owner's first named scenario: commit once, THEN
        unregister the selected Team from the SOURCE season, THEN replay
        the exact same fingerprint. Before #159 review round 3 this raised
        ``Team ... is not registered in the source season`` because the
        replay path re-resolved the plan (and so re-validated the source
        Season's CURRENT registrations) before ever consulting the ledger.
        The ledger is now consulted FIRST, keyed on the caller-supplied
        fingerprint, so this mutation of unrelated state can never affect
        an already-committed replay."""
        fp = self._preview()["copy_forward_fingerprint"]
        first = self._commit(fp)
        self.assertNotIn("error", first, first)
        unreg = self.api.unregister_team_from_season(
            self.fx.reg["id"], actor_id=ADMIN)
        self.assertNotIn("error", unreg, unreg)
        # The source Team really is no longer registered -- a FRESH preview
        # of the identical inputs now correctly refuses (proving the
        # mutation is real, not a no-op), while the REPLAY of the old,
        # already-successful fingerprint still succeeds.
        fresh_preview = self._preview()
        self.assertEqual(fresh_preview["error"]["code"], "validation_error")
        replay = self._commit(fp)
        self.assertNotIn("error", replay, replay)
        self.assertEqual(replay["season"], first["season"])
        self.assertEqual(replay["registrations"], first["registrations"])
        self.assertEqual(replay["totals"], first["totals"])

    def test_replay_survives_target_registration_deleted_after_commit(self):
        """Reproduces the owner's second named scenario: commit once, THEN
        unregister + delete the registration the commit itself created in
        the TARGET season, THEN replay. Before #159 review round 3 the
        replay path rebuilt its response by re-fetching each id in
        ``registration_ids`` from the live store, so a deleted row silently
        vanished from the response (``registrations: []``) while the SAME
        ledger row still reported ``rolled_forward: 1`` -- a self-
        contradictory result. The response is now a byte-accurate snapshot
        taken at commit time, so it is unaffected by the later delete."""
        fp = self._preview()["copy_forward_fingerprint"]
        first = self._commit(fp)
        self.assertNotIn("error", first, first)
        target_reg_id = first["registrations"][0]["id"]
        unreg = self.api.unregister_team_from_season(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", unreg, unreg)
        deleted = self.api.delete_season_team_registration(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", deleted, deleted)
        self.assertEqual(
            self.api.store.registrations_for_season(first["season"]["id"]),
            [])
        replay = self._commit(fp)
        self.assertNotIn("error", replay, replay)
        self.assertEqual(replay["season"], first["season"])
        self.assertEqual(replay["registrations"], first["registrations"])
        self.assertEqual(len(replay["registrations"]), 1)
        self.assertEqual(replay["totals"], first["totals"])
        self.assertEqual(replay["totals"]["rolled_forward"], 1)

    def test_target_season_delete_is_blocked_then_replay_stays_stable(self):
        """Reproduces the owner's third named scenario end to end: commit,
        clear every ORDINARY dependent of the generated Season (the
        registration the commit made, and its League binding), attempt to
        delete the Season anyway -- now blocked by the itemized
        ``copy_forward_commit`` dependency (#159 review round 3, structural
        change 2) -- then replay, which returns the ORIGINAL result,
        never ``season: null``."""
        fp = self._preview()["copy_forward_fingerprint"]
        first = self._commit(fp)
        self.assertNotIn("error", first, first)
        target_season_id = first["season"]["id"]
        target_reg_id = first["registrations"][0]["id"]
        unreg = self.api.unregister_team_from_season(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", unreg, unreg)
        deleted_reg = self.api.delete_season_team_registration(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", deleted_reg, deleted_reg)
        for ls in self.api.store.league_seasons_for_season(target_season_id):
            unbound = self.api.delete_league_season(ls.id, actor_id=ADMIN)
            self.assertNotIn("error", unbound, unbound)
        audits_before = len(self.api.store.all_setup_audit())
        delete_result = self.api.delete_season(
            target_season_id, actor_id=ADMIN)
        self.assertEqual(delete_result["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in
               delete_result["error"]["details"]["dependencies"]}
        self.assertEqual(deps, {"copy_forward_commit"})
        # Zero-write on block.
        self.assertIsNotNone(self.api.store.get_season(target_season_id))
        self.assertEqual(
            len(self.api.store.all_setup_audit()), audits_before)
        replay = self._commit(fp)
        self.assertNotIn("error", replay, replay)
        self.assertEqual(replay["season"], first["season"])
        self.assertIsNotNone(replay["season"])
        self.assertEqual(replay["season"]["id"], target_season_id)

    def test_commit_with_a_non_string_fingerprint_is_refused_not_500(self):
        """A malformed ``copy_forward_fingerprint`` (any JSON type other
        than a non-empty string) must never reach a store lookup -- binding
        a list/dict straight to a SQL parameter crashes the driver instead
        of raising a structured error. #159 review round 3 added an early
        ledger check keyed on the caller-supplied value directly, so this
        guards THAT new code path specifically (the OLD code only ever used
        an internally-computed fingerprint as a lookup key)."""
        fp = self._preview()["copy_forward_fingerprint"]
        for bad in (["not", "a", "string"], {"nope": 1}, 12345, True):
            res = self._commit(bad)
            self.assertIn("error", res, res)
            self.assertIn(res["error"]["code"],
                         ("validation_error", "schedule_conflict"))
        # The genuinely valid fingerprint still works afterward -- the
        # malformed attempts wrote nothing that could have consumed it.
        ok = self._commit(fp)
        self.assertNotIn("error", ok, ok)


class NewSeasonCopyForwardSqlTest(unittest.TestCase):
    """Runs each case against a REAL SqlStore -- SQLite always, PostgreSQL
    too when TEST_DATABASE_URL is set -- proving the zero-write guarantees
    over an actual transaction, not just InMemoryStore's own bookkeeping."""

    def _run(self, case):
        for name, url in _sql_backends():
            with self.subTest(backend=name):
                store = _fresh(url)
                try:
                    api = ApiService(store)
                    case(api, store)
                finally:
                    _teardown(store)

    def test_preview_writes_zero_domain_rows(self):
        def case(api, store):
            fx = _Fixture(api)
            before = len(store.all_seasons())
            before_regs = len(store.registrations_for_season(fx.src["id"]))
            resp = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            self.assertIn("copy_forward_fingerprint", resp)
            self.assertEqual(len(store.all_seasons()), before)
            self.assertEqual(
                len(store.registrations_for_season(fx.src["id"])),
                before_regs)
        self._run(case)

    def test_refused_commit_writes_zero_rows_missing_fingerprint(self):
        def case(api, store):
            fx = _Fixture(api)
            before_seasons = len(store.all_seasons())
            before_audits = len(store.all_setup_audit())
            res = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=None, actor_id=ADMIN)
            self.assertEqual(res["error"]["details"]["reason"],
                             "preview_required")
            self.assertEqual(len(store.all_seasons()), before_seasons)
            self.assertEqual(len(store.all_setup_audit()), before_audits)
        self._run(case)

    def test_refused_commit_writes_zero_rows_tampered_selection(self):
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            other_team = api.create_team(
                api.create_club("C2", actor_id=ADMIN)["id"], fx.div["id"],
                "Tampered", actor_id=ADMIN)
            api.register_team_for_season(
                fx.src["id"], other_team["id"], fx.div["id"], actor_id=ADMIN)
            before_seasons = len(store.all_seasons())
            before_regs_src = len(store.registrations_for_season(fx.src["id"]))
            before_audits = len(store.all_setup_audit())
            # Commit with the SAME (stale) fingerprint but a DIFFERENT,
            # tampered selections list naming a team never previewed.
            res = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[{"team_id": other_team["id"],
                            "league_id": fx.league["id"]}],
                copy_forward_fingerprint=preview["copy_forward_fingerprint"],
                actor_id=ADMIN)
            self.assertIn("error", res)
            self.assertEqual(len(store.all_seasons()), before_seasons)
            self.assertEqual(
                len(store.registrations_for_season(fx.src["id"])),
                before_regs_src)
            self.assertEqual(len(store.all_setup_audit()), before_audits)
        self._run(case)

    def test_commit_success_creates_exactly_one_season_and_one_registration(self):
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            before_seasons = len(store.all_seasons())
            res = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=preview["copy_forward_fingerprint"],
                actor_id=ADMIN)
            self.assertNotIn("error", res, res)
            self.assertEqual(len(store.all_seasons()), before_seasons + 1)
            new_regs = store.registrations_for_season(res["season"]["id"])
            self.assertEqual(len(new_regs), 1)
            self.assertEqual(new_regs[0].team_id, fx.team["id"])
            self.assertIsNone(new_regs[0].division_id)
            # Source Season's row count is completely unchanged.
            self.assertEqual(
                len(store.registrations_for_season(fx.src["id"])), 1)
        self._run(case)

    def test_second_commit_reusing_the_same_fingerprint_is_idempotent(self):
        """A commit fingerprint IS single-use in effect (#159 review round
        2 -- supersedes the old ``test_second_commit_reusing_a_valid_
        fingerprint_mints_a_second_season``, which pinned the exact
        double-submit/retry bug the owner ruled a real blocker, not an
        acceptable tradeoff).

        Unlike ``commit_ice_availability`` (#158) -- whose own idempotency
        comes from the WRITE TARGET having a natural dedup key (the
        ``(rink, start, end)`` unique index) -- this facade's write target,
        a brand new Season, has no such natural key of its own, so the
        idempotency here comes from an EXPLICIT ledger
        (``season_copy_forward_commits``, migration 053's UNIQUE index on
        ``copy_forward_fingerprint``) rather than the write target's own
        shape. A second commit that reuses the exact same, still-valid
        fingerprint does NOT mint a second Season and does NOT re-apply the
        registrations a second time -- it returns the SAME season /
        registrations / totals the first successful commit produced,
        exactly as if the client's original request had simply been
        re-delivered. See ``NewSeasonCopyForwardConcurrencyTest`` for the
        genuine two-connection race version of this same guarantee.
        """
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            fp = preview["copy_forward_fingerprint"]
            before_seasons = len(store.all_seasons())
            first = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", first, first)
            after_first_seasons = len(store.all_seasons())
            self.assertEqual(after_first_seasons, before_seasons + 1)
            after_first_audits = len(store.all_setup_audit())
            second = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", second, second)
            self.assertEqual(second["season"]["id"], first["season"]["id"])
            self.assertEqual(second["registrations"], first["registrations"])
            self.assertEqual(second["totals"], first["totals"])
            # No second Season, no second registration batch, no new audit
            # rows: the second attempt's own writes were fully rolled back
            # when its ledger insert lost the fingerprint race.
            self.assertEqual(len(store.all_seasons()), after_first_seasons)
            self.assertEqual(len(store.all_setup_audit()), after_first_audits)
            new_regs = store.registrations_for_season(first["season"]["id"])
            self.assertEqual(len(new_regs), 1)  # not duplicated
        self._run(case)

    # -- #159 review round 3 (owner P1), over REAL transactional stores
    # (SQLite always, PostgreSQL too when TEST_DATABASE_URL is set) --------
    def test_replay_survives_source_team_unregistered_after_commit(self):
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            fp = preview["copy_forward_fingerprint"]
            first = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", first, first)
            unreg = api.unregister_team_from_season(
                fx.reg["id"], actor_id=ADMIN)
            self.assertNotIn("error", unreg, unreg)
            replay = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", replay, replay)
            self.assertEqual(replay["season"], first["season"])
            self.assertEqual(replay["registrations"], first["registrations"])
            self.assertEqual(replay["totals"], first["totals"])
        self._run(case)

    def test_replay_survives_target_registration_deleted_after_commit(self):
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            fp = preview["copy_forward_fingerprint"]
            first = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", first, first)
            target_reg_id = first["registrations"][0]["id"]
            unreg = api.unregister_team_from_season(
                target_reg_id, actor_id=ADMIN)
            self.assertNotIn("error", unreg, unreg)
            deleted = api.delete_season_team_registration(
                target_reg_id, actor_id=ADMIN)
            self.assertNotIn("error", deleted, deleted)
            self.assertEqual(
                store.registrations_for_season(first["season"]["id"]), [])
            replay = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", replay, replay)
            self.assertEqual(replay["season"], first["season"])
            self.assertEqual(replay["registrations"], first["registrations"])
            self.assertEqual(len(replay["registrations"]), 1)
            self.assertEqual(replay["totals"]["rolled_forward"], 1)
        self._run(case)

    def test_target_season_delete_blocked_by_copy_forward_commit_never_raw_fk(self):
        """The specific SQL-only half of the owner's third scenario: a
        Season delete that would otherwise succeed (every ordinary
        dependent cleared) must be refused with the SAME itemized
        ``copy_forward_commit`` ``has_dependencies`` error SQLite AND
        PostgreSQL get -- migration 053's real ``season_copy_forward_
        commits.season_id`` foreign key must never be the thing that
        stops this delete; ``delete_season``'s pre-write dependency scan
        must stop it first, so the raw/generic ``foreign_key_violation``
        conflict this exact fixture reproduced pre-fix never surfaces."""
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            fp = preview["copy_forward_fingerprint"]
            first = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", first, first)
            target_season_id = first["season"]["id"]
            target_reg_id = first["registrations"][0]["id"]
            unreg = api.unregister_team_from_season(
                target_reg_id, actor_id=ADMIN)
            self.assertNotIn("error", unreg, unreg)
            deleted_reg = api.delete_season_team_registration(
                target_reg_id, actor_id=ADMIN)
            self.assertNotIn("error", deleted_reg, deleted_reg)
            for ls in store.league_seasons_for_season(target_season_id):
                unbound = api.delete_league_season(ls.id, actor_id=ADMIN)
                self.assertNotIn("error", unbound, unbound)
            audits_before = len(store.all_setup_audit())
            blocked = api.delete_season(target_season_id, actor_id=ADMIN)
            self.assertIn("error", blocked, blocked)
            self.assertEqual(blocked["error"]["code"], "has_dependencies")
            deps = {g["type"] for g in
                   blocked["error"]["details"]["dependencies"]}
            self.assertEqual(deps, {"copy_forward_commit"})
            self.assertIsNotNone(store.get_season(target_season_id))
            self.assertEqual(len(store.all_setup_audit()), audits_before)
            self.assertIsNotNone(
                store.get_season_copy_forward_commit_by_fingerprint(fp))
            # The ledger row was never touched by the refused delete -- a
            # replay of the SAME fingerprint right afterward still returns
            # the ORIGINAL stable result.
            replay = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=fp, actor_id=ADMIN)
            self.assertNotIn("error", replay, replay)
            self.assertEqual(replay["season"]["id"], target_season_id)
            self.assertIsNotNone(replay["season"])
        self._run(case)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class NewSeasonCopyForwardConcurrencyTest(unittest.TestCase):
    """Real two-connection PostgreSQL races (#159 review round 2): two
    GENUINELY concurrent ``commit_new_season_copy_forward`` calls -- two
    real database connections, launched together via ``threading.Barrier``
    so neither starts before the other is ready, not two sequential calls
    -- carrying the IDENTICAL ``copy_forward_fingerprint``. Exactly one
    Season is ever created for that fingerprint; the loser receives the
    winner's exact result, never a second Season, never a partial/torn
    write. Mirrors ``test_season_lifecycle_concurrency.py``'s
    ``SeasonArchiveRaceTest`` barrier pattern -- the house pattern for a
    real two-connection race in this codebase."""

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _seed(self):
        store = SqlStore(self.url)
        api = ApiService(store)
        fx = _Fixture(api)
        sel = [fx.selection()]
        preview = api.preview_new_season_copy_forward(
            program_id=fx.program["id"], name="2026-27",
            source_season_id=fx.src["id"], selections=sel, actor_id=ADMIN)
        return fx, sel, preview["copy_forward_fingerprint"]

    def test_concurrent_commits_with_the_same_fingerprint_create_exactly_one_season(self):
        fx, sel, fp = self._seed()
        before_seasons = {s.id for s in SqlStore(self.url).all_seasons()}
        api_a = ApiService(SqlStore(self.url))
        api_b = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def commit(api, key):
            barrier.wait()
            results[key] = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"], selections=sel,
                copy_forward_fingerprint=fp, actor_id=ADMIN)

        ta = threading.Thread(target=commit, args=(api_a, "a"))
        tb = threading.Thread(target=commit, args=(api_b, "b"))
        ta.start(); tb.start(); ta.join(20); tb.join(20)

        # Neither side ever sees an error: the loser gets the winner's
        # result, not a refusal.
        self.assertNotIn("error", results.get("a", {}), results)
        self.assertNotIn("error", results.get("b", {}), results)
        # Both calls report the SAME Season -- never two distinct ones,
        # regardless of which connection's INSERT actually won the race.
        self.assertEqual(results["a"]["season"]["id"],
                         results["b"]["season"]["id"], results)
        self.assertEqual(results["a"]["totals"], results["b"]["totals"])
        self.assertEqual(results["a"]["registrations"],
                         results["b"]["registrations"])
        check = SqlStore(self.url)
        new_season_ids = {s.id for s in check.all_seasons()} - before_seasons
        self.assertEqual(len(new_season_ids), 1, new_season_ids)  # exactly one
        won_season_id = next(iter(new_season_ids))
        self.assertEqual(results["a"]["season"]["id"], won_season_id)
        # Registrations applied exactly once, not twice.
        regs = check.registrations_for_season(won_season_id)
        self.assertEqual(len(regs), 1, regs)
        # Exactly one ledger row for this fingerprint, naming the winner.
        ledger = check.get_season_copy_forward_commit_by_fingerprint(fp)
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger.season_id, won_season_id)
        # No orphaned audit trail from the loser's rolled-back attempt: the
        # winner's own season_created / registered / committed rows are the
        # only ones naming this Season.
        season_audits = [a for a in check.all_setup_audit()
                         if a.entity_id == won_season_id
                         and a.action == "season_created"]
        self.assertEqual(len(season_audits), 1, season_audits)

    def test_concurrent_commit_racing_a_sequential_replay_still_one_season(self):
        """The reverse arrival order (#159 review round 2's "both possible
        arrival orders"): a commit already succeeded SEQUENTIALLY before the
        second one starts, so by the time this test's own two threads race,
        one of them is really racing an ALREADY-COMMITTED fingerprint --
        exactly the sequential-replay case, but launched through the same
        concurrent barrier harness to prove it converges the same way even
        under real thread/connection scheduling."""
        fx, sel, fp = self._seed()
        first = ApiService(SqlStore(self.url)).commit_new_season_copy_forward(
            program_id=fx.program["id"], name="2026-27",
            source_season_id=fx.src["id"], selections=sel,
            copy_forward_fingerprint=fp, actor_id=ADMIN)
        self.assertNotIn("error", first, first)
        before_seasons = {s.id for s in SqlStore(self.url).all_seasons()}
        api_a = ApiService(SqlStore(self.url))
        api_b = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def commit(api, key):
            barrier.wait()
            results[key] = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"], selections=sel,
                copy_forward_fingerprint=fp, actor_id=ADMIN)

        ta = threading.Thread(target=commit, args=(api_a, "a"))
        tb = threading.Thread(target=commit, args=(api_b, "b"))
        ta.start(); tb.start(); ta.join(20); tb.join(20)

        self.assertNotIn("error", results.get("a", {}), results)
        self.assertNotIn("error", results.get("b", {}), results)
        self.assertEqual(results["a"]["season"]["id"], first["season"]["id"])
        self.assertEqual(results["b"]["season"]["id"], first["season"]["id"])
        check = SqlStore(self.url)
        # No NEW season beyond the one the sequential first commit created.
        self.assertEqual({s.id for s in check.all_seasons()}, before_seasons)

    def test_delete_vs_replay_race_delete_stays_blocked_replay_stays_stable(self):
        """#159 review round 3's required delete-vs-replay race: a real
        two-connection race between (A) replaying an already-committed
        fingerprint and (B) attempting to delete the Season that commit
        produced, launched together via ``threading.Barrier`` so neither
        starts before the other is ready. The ledger row that blocks (B) is
        written in the SAME transaction as the Season and its registration
        (B) needs cleared first, so there is no window in which the Season
        exists without it -- (B) must be refused regardless of which
        thread's connection is scheduled first, and (A)'s replay -- which
        touches neither ``seasons`` nor ``season_team_registrations`` at
        all -- must return the exact original result regardless of (B)'s
        outcome or timing."""
        fx, sel, fp = self._seed()
        seed_api = ApiService(SqlStore(self.url))
        first = seed_api.commit_new_season_copy_forward(
            program_id=fx.program["id"], name="2026-27",
            source_season_id=fx.src["id"], selections=sel,
            copy_forward_fingerprint=fp, actor_id=ADMIN)
        self.assertNotIn("error", first, first)
        target_season_id = first["season"]["id"]
        target_reg_id = first["registrations"][0]["id"]
        # Clear the ordinary dependents SEQUENTIALLY, before the race, so
        # only the copy_forward_commit ledger dependency remains to block
        # (B) -- isolates the race to the property this test is actually
        # proving.
        unreg = seed_api.unregister_team_from_season(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", unreg, unreg)
        deleted_reg = seed_api.delete_season_team_registration(
            target_reg_id, actor_id=ADMIN)
        self.assertNotIn("error", deleted_reg, deleted_reg)
        for ls in SqlStore(self.url).league_seasons_for_season(
                target_season_id):
            unbound = seed_api.delete_league_season(ls.id, actor_id=ADMIN)
            self.assertNotIn("error", unbound, unbound)

        before_seasons = {s.id for s in SqlStore(self.url).all_seasons()}
        before_audits = len(SqlStore(self.url).all_setup_audit())
        api_replay = ApiService(SqlStore(self.url))
        api_delete = ApiService(SqlStore(self.url))
        barrier = threading.Barrier(2)
        results = {}

        def replay():
            barrier.wait()
            results["replay"] = api_replay.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"], selections=sel,
                copy_forward_fingerprint=fp, actor_id=ADMIN)

        def delete():
            barrier.wait()
            results["delete"] = api_delete.delete_season(
                target_season_id, actor_id=ADMIN)

        t_replay = threading.Thread(target=replay)
        t_delete = threading.Thread(target=delete)
        t_replay.start(); t_delete.start()
        t_replay.join(20); t_delete.join(20)

        replay_result = results["replay"]
        delete_result = results["delete"]
        self.assertNotIn("error", replay_result, replay_result)
        self.assertEqual(replay_result["season"], first["season"])
        # Compare the STABLE registration fields the snapshot owns (id,
        # team_id, division_id, active), not the full dict: this test
        # deliberately unbound the LeagueSeason above (the only way to
        # legitimately clear a Season's "level" dependent, so the delete
        # attempt below is blocked by copy_forward_commit ALONE). The
        # facade's ``_registration_dict`` resolves ``season_id``/
        # ``league_id`` from ``league_season_id`` with a LIVE store lookup
        # on EVERY call, replay or fresh -- a general, pre-existing
        # property of that denormalization step, not something this
        # round's immutable-snapshot fix touches or is scoped to change
        # -- so those two derived fields correctly read back None once the
        # binding they are resolved from is gone, on a fresh call exactly
        # as much as on a replay.
        self.assertEqual(len(replay_result["registrations"]),
                         len(first["registrations"]))
        for got, want in zip(replay_result["registrations"],
                             first["registrations"]):
            self.assertEqual(got["id"], want["id"])
            self.assertEqual(got["team_id"], want["team_id"])
            self.assertEqual(got["division_id"], want["division_id"])
            self.assertEqual(got["active"], want["active"])
        self.assertEqual(replay_result["totals"], first["totals"])
        self.assertIn("error", delete_result, delete_result)
        self.assertEqual(delete_result["error"]["code"], "has_dependencies")
        deps = {g["type"] for g in
               delete_result["error"]["details"]["dependencies"]}
        self.assertEqual(deps, {"copy_forward_commit"})
        check = SqlStore(self.url)
        # No new/removed Season, no new audit row from the blocked delete.
        self.assertEqual({s.id for s in check.all_seasons()}, before_seasons)
        self.assertIsNotNone(check.get_season(target_season_id))
        self.assertEqual(len(check.all_setup_audit()), before_audits)
        self.assertIsNotNone(
            check.get_season_copy_forward_commit_by_fingerprint(fp))


class NewSeasonCopyForwardHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    def _post(self, path, body, role):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "X-Demo-Role": role})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _session(self, opener, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _operator(self, username):
        """A real signed-in League Admin session (#409): every guarded
        create below is authorized against the Program the operator
        actually CHOSE via /api/context, matching test_season_rollover.py's
        own ``_operator``."""
        if STATE.api.store.get_user_account_by_username(username) is None:
            STATE.api.accounts.create_account(
                username, "demo", Role.LEAGUE_ADMIN, scope={},
                actor_id="test_seed")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, body = self._session(opener, "/api/auth/login",
                                     {"username": username, "password": "demo"})
        self.assertEqual(status, 200, body)
        return opener

    def _hierarchy(self, tag):
        """Program + source Season (League/Division/Team/registration) built
        by a signed-in operator that SELECTS the Program (#409), matching
        this route's PROGRAM-AXIS shape."""
        c = self._operator(f"cf_admin_{tag}")
        _, program = self._session(c, "/api/setup/league", {"name": f"P{tag}"})
        self._session(c, "/api/context", {"program_id": program["id"]})
        _, src = self._session(c, "/api/setup/season",
                               {"league_id": program["id"], "name": f"S{tag}"})
        self._session(c, "/api/context",
                      {"program_id": program["id"], "season_id": src["id"]})
        _, league = self._session(
            c, "/api/v2/setup/league",
            {"season_id": src["id"], "name": f"Comp{tag}"})
        _, div = self._session(
            c, "/api/v2/setup/division",
            {"league_id": league["id"], "name": f"Div{tag}"})
        _, club = self._session(c, "/api/setup/club", {"name": f"C{tag}"})
        _, team = self._session(c, "/api/setup/team",
                                {"club_id": club["id"], "division_id": div["id"],
                                 "name": f"T{tag}"})
        status, reg = self._session(
            c, f"/api/setup/seasons/{src['id']}/team-registrations",
            {"team_id": team["id"], "division_id": div["id"]})
        self.assertEqual(status, 200, reg)
        return c, program, src, league, team

    def _preview_body(self, program, src, league, team, **overrides):
        body = {
            "program_id": program["id"], "name": "Next Season",
            "source_season_id": src["id"],
            "selections": [{"team_id": team["id"], "league_id": league["id"]}],
        }
        body.update(overrides)
        return body

    def test_admin_can_preview_then_commit_and_viewer_cannot(self):
        c, program, src, league, team = self._hierarchy("H1")
        body = self._preview_body(program, src, league, team)
        # Viewer is forbidden on BOTH routes.
        status, resp = self._post(
            "/api/v2/setup/seasons/copy-forward/preview", body, "viewer")
        self.assertEqual(status, 403)
        self.assertEqual(resp["error"]["details"]["required"], "manage_setup")
        status, resp = self._post(
            "/api/v2/setup/seasons/copy-forward/commit", body, "viewer")
        self.assertEqual(status, 403)

        # League Admin previews, then commits with the returned fingerprint.
        status, preview = self._session(
            c, "/api/v2/setup/seasons/copy-forward/preview", body)
        self.assertEqual(status, 200, preview)
        self.assertIn("copy_forward_fingerprint", preview)

        commit_body = dict(body, copy_forward_fingerprint=preview[
            "copy_forward_fingerprint"])
        status, committed = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)
        self.assertEqual(status, 200, committed)
        self.assertTrue(committed["committed"])
        self.assertEqual(committed["season"]["program_id"], program["id"])
        self.assertEqual(len(committed["registrations"]), 1)

    def test_sequential_commit_replay_over_http_is_idempotent(self):
        """#159 review round 2: two SEQUENTIAL authenticated HTTP commits
        carrying the SAME copy_forward_fingerprint return the SAME Season,
        not two distinct ones -- the exact double-submit shape the owner's
        review demonstrated as a live bug against this route."""
        c, program, src, league, team = self._hierarchy("H4")
        body = self._preview_body(program, src, league, team)
        status, preview = self._session(
            c, "/api/v2/setup/seasons/copy-forward/preview", body)
        self.assertEqual(status, 200, preview)
        commit_body = dict(body, copy_forward_fingerprint=preview[
            "copy_forward_fingerprint"])
        status1, first = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)
        self.assertEqual(status1, 200, first)
        status2, second = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)
        self.assertEqual(status2, 200, second)
        self.assertEqual(first["season"]["id"], second["season"]["id"])
        self.assertEqual(first["registrations"], second["registrations"])
        self.assertEqual(first["totals"], second["totals"])
        program_seasons = [s for s in STATE.api.store.all_seasons()
                          if s.program_id == program["id"]
                          and s.id != src["id"]]
        self.assertEqual(len(program_seasons), 1, program_seasons)

    def test_replay_over_http_survives_source_team_unregistered(self):
        """#159 review round 3, over REAL HTTP: commit once, THEN
        unregister the source Team through the real, authenticated
        ``season-team-registration/{id}/remove`` route, THEN replay the
        exact same authenticated commit request. Before this round the
        replay path re-validated the source Season's CURRENT registrations
        before ever checking the ledger, so this returned a 400
        validation_error instead of the original committed result."""
        c, program, src, league, team = self._hierarchy("H8")
        body = self._preview_body(program, src, league, team)
        status, preview = self._session(
            c, "/api/v2/setup/seasons/copy-forward/preview", body)
        self.assertEqual(status, 200, preview)
        commit_body = dict(body, copy_forward_fingerprint=preview[
            "copy_forward_fingerprint"])
        status1, first = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)
        self.assertEqual(status1, 200, first)

        src_reg = next(r for r in
                       STATE.api.store.registrations_for_season(src["id"])
                       if r.team_id == team["id"])
        status_u, unreg = self._session(
            c, f"/api/v2/setup/season-team-registration/{src_reg.id}/remove",
            {})
        self.assertEqual(status_u, 200, unreg)

        status2, replay = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)
        self.assertEqual(status2, 200, replay)
        self.assertEqual(replay["season"], first["season"])
        self.assertEqual(replay["registrations"], first["registrations"])
        self.assertEqual(replay["totals"], first["totals"])
        program_seasons = [s for s in STATE.api.store.all_seasons()
                          if s.program_id == program["id"]
                          and s.id != src["id"]]
        self.assertEqual(len(program_seasons), 1, program_seasons)

    def test_concurrent_commit_replay_over_http_is_idempotent(self):
        """#159 review round 2, GENUINE concurrent replay: two real
        concurrent authenticated HTTP requests (two threads, two separate
        client connections) carrying the SAME copy_forward_fingerprint --
        not two sequential calls -- still produce exactly one Season, and
        both responses name it."""
        c, program, src, league, team = self._hierarchy("H6")
        body = self._preview_body(program, src, league, team)
        status, preview = self._session(
            c, "/api/v2/setup/seasons/copy-forward/preview", body)
        self.assertEqual(status, 200, preview)
        commit_body = dict(body, copy_forward_fingerprint=preview[
            "copy_forward_fingerprint"])
        barrier = threading.Barrier(2)
        results = {}

        def commit(key):
            barrier.wait()
            results[key] = self._session(
                c, "/api/v2/setup/seasons/copy-forward/commit", commit_body)

        ta = threading.Thread(target=commit, args=("a",))
        tb = threading.Thread(target=commit, args=("b",))
        ta.start(); tb.start(); ta.join(20); tb.join(20)

        status_a, body_a = results["a"]
        status_b, body_b = results["b"]
        self.assertEqual(status_a, 200, body_a)
        self.assertEqual(status_b, 200, body_b)
        self.assertEqual(body_a["season"]["id"], body_b["season"]["id"])
        self.assertEqual(body_a["totals"], body_b["totals"])
        program_seasons = [s for s in STATE.api.store.all_seasons()
                          if s.program_id == program["id"]
                          and s.id != src["id"]]
        self.assertEqual(len(program_seasons), 1, program_seasons)

    def test_commit_without_a_prior_preview_is_refused(self):
        c, program, src, league, team = self._hierarchy("H2")
        body = self._preview_body(
            program, src, league, team,
            copy_forward_fingerprint="fabricated-token")
        status, resp = self._session(
            c, "/api/v2/setup/seasons/copy-forward/commit", body)
        # schedule_conflict has no explicit ERROR_HTTP_STATUS entry, so it
        # falls to _send_api's 400 default -- the SAME status
        # commit_ice_availability's own preview_mismatch refusal gets.
        self.assertEqual(status, 400, resp)
        self.assertEqual(resp["error"]["details"]["reason"], "preview_mismatch")

    def test_no_active_context_is_refused(self):
        # A signed-in operator who has never selected a Program at all.
        username = "cf_admin_noctx"
        if STATE.api.store.get_user_account_by_username(username) is None:
            STATE.api.accounts.create_account(
                username, "demo", Role.LEAGUE_ADMIN, scope={},
                actor_id="test_seed")
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        status, _ = self._session(opener, "/api/auth/login",
                                  {"username": username, "password": "demo"})
        self.assertEqual(status, 200)
        body = {"program_id": "prog_x", "name": "N", "source_season_id": "s_x",
               "selections": [{"team_id": "t_x", "league_id": "l_x"}]}
        status, resp = self._session(
            opener, "/api/v2/setup/seasons/copy-forward/preview", body)
        self.assertEqual(status, 409, resp)
        self.assertEqual(resp["error"]["code"], "active_context_required")

    def test_malformed_body_returns_400_not_500(self):
        c, program, src, league, team = self._hierarchy("H3")
        for bad_selections in (
            "not-a-list",
            [{"team_id": ["t1"], "league_id": league["id"]}],
            [{"team_id": team["id"], "division_id": {"x": 1}}],
        ):
            body = self._preview_body(
                program, src, league, team, selections=bad_selections)
            status, resp = self._session(
                c, "/api/v2/setup/seasons/copy-forward/preview", body)
            self.assertEqual(status, 400,
                             f"selections={bad_selections!r} -> {status} {resp}")
            self.assertEqual(resp["error"]["code"], "validation_error")


if __name__ == "__main__":
    unittest.main()
