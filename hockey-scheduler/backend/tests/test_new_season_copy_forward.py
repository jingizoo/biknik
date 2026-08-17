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

    def test_second_commit_reusing_a_valid_fingerprint_mints_a_second_season(self):
        """A commit fingerprint is NOT single-use / consumed on success.

        Unlike ``commit_ice_availability`` (#158) -- whose own idempotency
        comes from the WRITE TARGET having a natural dedup key (the
        ``(rink, start, end)`` unique index, so a rerun of the identical
        template is a no-op skip) -- this facade's write target is a brand
        new Season, which has no such natural key: two Seasons created with
        identical inputs are, by the domain model, two genuinely different
        rows (the same as calling ``create_season`` itself twice). So a
        second commit that reuses the exact same, still-valid fingerprint
        does NOT skip -- it passes all three preview-binding gates again
        (nothing about them is one-shot) and mints a SECOND, DISTINCT
        Season with its own copy of the carried-forward registrations. This
        is deliberate ("each commit mints its own new Season by design"),
        not a bypass of the preview-required contract: every commit here,
        including this second one, is still preceded by a genuine preview
        by the same actor. Guarding against an accidental double-submit
        (double-click, client retry) would need a SEPARATE, single-use
        consumption mechanism this slice does not build.
        """
        def case(api, store):
            fx = _Fixture(api)
            preview = api.preview_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()], actor_id=ADMIN)
            first = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=preview["copy_forward_fingerprint"],
                actor_id=ADMIN)
            self.assertNotIn("error", first, first)
            second = api.commit_new_season_copy_forward(
                program_id=fx.program["id"], name="2026-27",
                source_season_id=fx.src["id"],
                selections=[fx.selection()],
                copy_forward_fingerprint=preview["copy_forward_fingerprint"],
                actor_id=ADMIN)
            self.assertNotIn("error", second, second)
            self.assertNotEqual(first["season"]["id"], second["season"]["id"])
        self._run(case)


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
