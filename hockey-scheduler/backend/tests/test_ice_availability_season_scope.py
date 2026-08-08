"""Ice Availability Builder cross-Program Season authorization — real
authenticated HTTP (#158 route, the #369/#371/#372 defect class).

THE QUESTION: `/api/setup/ice-availability/preview` and `.../commit` take
`season_id` from the CLIENT BODY. The route (`web/server.py`, the
`path in ("/api/setup/ice-availability/preview", ".../commit")` branch) passes
`actor_id=user_id` but NOT `role`/`scope`, and is matched BEFORE the generic
`/api/setup/` dispatcher — so it never reaches `_handle_setup_v2`, which is
where the `_guarded_mutation` / `setup_target_accessible` target-record gate
lives. Can an operator holding MANAGE_ARENA create ice in a Season belonging
to a Program they are not working in?

WHY THE EXISTING COVERAGE DOES NOT ANSWER IT. `IceAvailabilityHttpAuthzTest`
in test_ice_availability.py proves the route is gated to MANAGE_ARENA — a
PERMISSION check ("does this role hold the capability?"). That is a different
question from a SCOPE check ("is this Season one this caller may act on?"),
and #369 named the distinction explicitly: *permission to use a setup
capability is not authority over every record in the installation.* Every
assertion in that class stays green while the hole below is open.

WHAT "AUTHORIZED FOR PROGRAM A" MEANS HERE. Under the current account model
(`services/context_scope.py`) `LEAGUE_ADMIN` and `ARENA_MANAGER` are
`_GLOBAL_ROLES` — `authorized_program_ids` returns EVERY Program — and
`AccountService._ALLOWED_SCOPE_KEYS` gives neither role any scope key at all,
so no per-Program binding can even be persisted on the account. The caller's
persisted ACTIVE CONTEXT is therefore the only Program authorization signal
these roles have, and it is exactly the signal `setup_target_accessible`
judges against (`resolve_with_league`, all three axes, null Program failing
CLOSED). So "authorized only for Program A" is expressed here as an operator
whose active context IS Program A / Season A, which is what every other setup
mutation is measured against.

THE MATRIX, over a real `ThreadingHTTPServer` running the real `Handler` with
real session cookies — the transport is part of what is under test, because
the gap is in the route layer:

  * Arena Manager  (MANAGE_ARENA, no MANAGE_SETUP)  — positive control, then probe
  * League Admin   (MANAGE_ARENA + MANAGE_SETUP)    — positive control, then probe
  * Coach          (no MANAGE_ARENA)                — proves the PERMISSION gate
                                                      works, so a refusal above
                                                      would be distinguishable
                                                      from this one

Each case asserts PERSISTED STATE (the foreign rink's ice-slot count before
and after), not just the response code: a 200 with no write and a 403 with a
write are both possible and both matter.

Two further cases pin the shape of the defect rather than just its existence:

  * ``test_foreign_and_nonexistent_season_are_not_distinguishable`` — #372's
    convention is that an inaccessible target and a nonexistent one must be
    byte-identical so the error cannot be used as an existence oracle.
  * ``test_gated_sibling_route_refuses_the_same_foreign_season`` — the CONTROL
    that proves the gate exists and works: `/api/v2/setup/seasons/<id>/archive`
    names a Season the same way and IS routed through `_guarded_mutation`.
    It runs against the SAME foreign Season id, in the same session, in the
    same context. This one is expected to PASS; it is what the ice-availability
    route should look like.

DO NOT "fix" this by deleting the failing assertions. The correct gate is an
owner decision — the active-context tuple (what `setup_target_accessible`
uses) versus merely the caller's authorized Programs — and the two differ for
exactly these global roles.
"""

import json
import os
import threading
import time
import unittest
import uuid
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from datetime import datetime, timezone

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.domain.setup_models import ActiveContext
from hockey_scheduler.store import InMemoryStore, SqlStore

TZ = "America/Toronto"

PREVIEW = "/api/setup/ice-availability/preview"
COMMIT = "/api/setup/ice-availability/commit"

# An id that cannot exist, for the existence-oracle comparison.
NONEXISTENT_SEASON = "season_totally_nonexistent_9999999"


def _template(season_id, rink_id, **over):
    """A Tue(1)/Thu(3) 18:00-22:00 one-week template => 3 games/day => 6 slots.

    Same shape as test_ice_availability.py's `_template`, retargetable at an
    arbitrary Season/Rink so the foreign-Program probe sends a request that is
    valid in every respect EXCEPT whose Season it names.
    """
    base = dict(season_id=season_id, rink_ids=[rink_id], weekdays=[1, 3],
                start_local="18:00", end_local="22:00",
                start_date="2026-09-01", end_date="2026-09-07",
                playable_minutes=60, turnover_minutes=15)
    base.update(over)
    return base


class IceAvailabilitySeasonScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv = __import__("hockey_scheduler.web.server", fromlist=["x"])
        cls.srv = srv
        srv.STATE.reset()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=5)
        cls.httpd.server_close()

    # -- harness (copied idiom from test_players_http_scope.py) -------------
    def _client(self):
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def _req(self, opener, method, path, body=None):
        """Returns (status, raw_text, parsed) — the RAW body is what the
        information-leak assertions check, so a foreign Program's Season /
        Venue / Rink name is caught anywhere in the payload."""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req) as r:
                raw = r.read()
                return r.status, raw.decode(), json.loads(raw or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, raw.decode(), json.loads(raw or b"{}")

    def _login(self, username):
        c = self._client()
        status, raw, _ = self._req(c, "POST", "/api/auth/login",
                                   {"username": username, "password": "demo"})
        self.assertEqual(status, 200, raw)
        return c

    def _use(self, c, program_id, season_id):
        status, raw, _ = self._req(c, "POST", "/api/context",
                                   {"program_id": program_id,
                                    "season_id": season_id})
        self.assertEqual(status, 200, raw)

    # -- fixture ------------------------------------------------------------
    def _program(self, tag):
        """One Program with a Season, a Venue holding a real SeasonVenueAccess
        grant, and a Rink under it — the minimum for a template to resolve and
        commit. Built through the setup service (the idiom
        `test_commit_binding_over_http` already uses) because these are
        FIXTURES, not the surface under test.

        `tag` is embedded in every name so a leak of another Program's
        inventory shows up as a plain substring of the raw response.
        """
        svc = self.srv.STATE.api.setup
        program = svc.create_program(f"{tag} Program", timezone_name=TZ)
        season = svc.create_season(program.id, f"{tag} Season")
        venue = svc.create_venue(f"{tag} Venue", league_id=program.id)
        svc.grant_season_venue_access(season.id, venue.id)
        rink = svc.create_rink(venue.id, f"{tag} Sheet")
        return {"tag": tag, "program_id": program.id, "season_id": season.id,
                "venue_id": venue.id, "rink_id": rink.id}

    def _two_programs(self, tag):
        """Programs A and B, fully independent. A fresh pair PER TEST, so each
        case proves its own write against a rink no other case has touched —
        an inherited slot count would let a real write hide behind another
        case's `duplicate_skipped`."""
        return self._program(f"{tag}A"), self._program(f"{tag}B")

    def _slots(self, rink_id):
        return sum(1 for s in self.srv.STATE.api.store.all_ice_slots()
                   if s.rink_id == rink_id)

    def _fresh_operator(self, tag, role):
        """A BRAND-NEW account with no persisted ActiveContext.

        `srv.STATE` is shared across the class, so reusing a demo login means
        an earlier case's `_use(...)` has already saved a selection for that
        account — the no-context cases would silently inherit it and prove
        nothing. Isolating the identity is stronger than clearing state after
        the fact: there is no ordering for a later case to depend on, and no
        cleanup that can be forgotten.
        """
        username = f"{tag}_{uuid.uuid4().hex[:10]}"
        self.srv.STATE.api.accounts.create_account(username, "demo", role)
        return username

    def _sibling_season(self, home, tag):
        """A SECOND Season inside the SAME Program, sharing its Venue.

        The Program ceiling cannot refuse this one: it is the caller's own
        Program, the caller is authorized for it, and switching to it would be
        entirely legitimate. Only the EXACT-tuple rule can — which is what
        prerequisite 2 asks to be proven.
        """
        svc = self.srv.STATE.api.setup
        season = svc.create_season(home["program_id"], f"{tag} Sibling Season")
        svc.grant_season_venue_access(season.id, home["venue_id"])
        return season.id

    def _audit_count(self):
        """Total setup-audit rows. A refusal must add none: a 4xx/409 that
        still recorded an attempt would mean the write path was entered."""
        return len(self.srv.STATE.api.store.all_setup_audit())

    # -- the reusable case --------------------------------------------------
    def _positive_control(self, c, home):
        """ANTI-VACUITY: this operator, in this context, really can build ice
        in its OWN Season — so a refusal in the probe below is a scope
        decision and not a malformed body, a missing venue grant, or a broken
        fixture."""
        status, raw, pv = self._req(c, "POST", PREVIEW,
                                    _template(home["season_id"], home["rink_id"]))
        self.assertEqual(status, 200, f"positive-control preview: {raw}")
        fingerprint = pv.get("template_fingerprint")
        self.assertTrue(fingerprint, raw)
        self.assertEqual(pv["totals"]["new"], 6, raw)

        before = self._slots(home["rink_id"])
        status, raw, ok = self._req(
            c, "POST", COMMIT,
            dict(_template(home["season_id"], home["rink_id"]),
                 template_fingerprint=fingerprint))
        self.assertEqual(status, 200, f"positive-control commit: {raw}")
        self.assertEqual(ok["totals"]["created"], 6, raw)
        self.assertEqual(self._slots(home["rink_id"]), before + 6,
                         "positive control wrote no ice — the fixture is broken "
                         "and every refusal below would be vacuous")

    def _probe_foreign(self, c, foreign):
        """Preview a FOREIGN Program's Season, then — whatever preview returned
        — attempt the commit with the fingerprint it produced. Returns the two
        (status, raw) pairs plus the before/after slot counts on the foreign
        rink, so the caller asserts on PERSISTED STATE and not only on codes."""
        template = _template(foreign["season_id"], foreign["rink_id"])
        pv_status, pv_raw, pv = self._req(c, "POST", PREVIEW, template)

        before = self._slots(foreign["rink_id"])
        commit_body = dict(template)
        if isinstance(pv, dict) and pv.get("template_fingerprint"):
            commit_body["template_fingerprint"] = pv["template_fingerprint"]
        cm_status, cm_raw, _ = self._req(c, "POST", COMMIT, commit_body)
        after = self._slots(foreign["rink_id"])
        return (pv_status, pv_raw), (cm_status, cm_raw), before, after

    def _assert_foreign_season_refused(self, persona, c, foreign):
        (pv_status, pv_raw), (cm_status, cm_raw), before, after = \
            self._probe_foreign(c, foreign)

        report = (f"\n  persona            : {persona}"
                  f"\n  foreign season_id  : {foreign['season_id']}"
                  f"\n  preview  -> {pv_status} {pv_raw}"
                  f"\n  commit   -> {cm_status} {cm_raw}"
                  f"\n  foreign rink slots : {before} -> {after}")

        # The WRITE is the security question: whatever the status line says,
        # nothing may land in another Program's Season.
        self.assertEqual(
            after, before,
            f"{persona} created ice in a Season outside its active "
            f"Program.{report}")

        # And the response must be the generic not-found every other setup
        # mutation sends for an out-of-scope target (`_refuse_target`).
        self.assertEqual(pv_status, 404,
                         f"{persona} preview was not refused.{report}")
        self.assertEqual(cm_status, 404,
                         f"{persona} commit was not refused.{report}")

        # A refusal must not echo the foreign Program's inventory back.
        for name in (f"{foreign['tag']} Season", f"{foreign['tag']} Venue",
                     f"{foreign['tag']} Sheet"):
            self.assertNotIn(name, pv_raw,
                             f"{persona} preview leaked {name!r}.{report}")
            self.assertNotIn(name, cm_raw,
                             f"{persona} commit leaked {name!r}.{report}")

    # -- Arena Manager ------------------------------------------------------
    def test_arena_manager_builds_ice_in_its_own_season(self):
        """Positive control, standing on its own so a green run still records
        that the capability works when it is supposed to."""
        home, _ = self._two_programs("AmOwn")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)

    def test_arena_manager_cannot_build_ice_in_a_foreign_program_season(self):
        home, foreign = self._two_programs("AmForeign")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)          # anti-vacuity, same session
        self._assert_foreign_season_refused("Arena Manager", c, foreign)

    # -- League Admin -------------------------------------------------------
    def test_league_admin_cannot_build_ice_in_a_foreign_program_season(self):
        """League Admin also holds MANAGE_ARENA (`domain/roles.py`), so it
        reaches the same handler. Running the matrix for both is what
        distinguishes "no scope check on this route" from "this one role is
        over-privileged"."""
        home, foreign = self._two_programs("LaForeign")
        c = self._login("admin")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)
        self._assert_foreign_season_refused("League Admin", c, foreign)

    # -- Coach: the PERMISSION gate, which does work -------------------------
    def test_coach_lacks_manage_arena_on_both_seasons(self):
        """The control that separates the two failure modes. A Coach holds no
        MANAGE_ARENA, is refused 403 on its own Program's Season AND on a
        foreign one, and writes nothing either way — so the 200s above are
        specifically a missing SCOPE check, not a missing permission check."""
        home, foreign = self._two_programs("CoachProbe")
        c = self._login("coach")

        for label, target in (("own", home), ("foreign", foreign)):
            before = self._slots(target["rink_id"])
            template = _template(target["season_id"], target["rink_id"])
            status, raw, body = self._req(c, "POST", PREVIEW, template)
            self.assertEqual(status, 403, f"coach preview ({label}): {raw}")
            self.assertEqual(body["error"]["code"], "forbidden", raw)
            self.assertEqual(body["error"]["details"]["required"],
                             "manage_arena", raw)

            status, raw, _ = self._req(c, "POST", COMMIT, template)
            self.assertEqual(status, 403, f"coach commit ({label}): {raw}")
            self.assertEqual(self._slots(target["rink_id"]), before,
                             f"coach wrote ice into the {label} Season: {raw}")

    # -- existence oracle ---------------------------------------------------
    def test_foreign_and_nonexistent_season_are_not_distinguishable(self):
        """#372's convention: an inaccessible target and a nonexistent one must
        be byte-identical, or the error is an existence oracle for another
        Program's records. Compared on the SAME route, same session, same
        context, with only `season_id` differing."""
        home, foreign = self._two_programs("Oracle")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])

        f_status, f_raw, _ = self._req(
            c, "POST", PREVIEW,
            _template(foreign["season_id"], foreign["rink_id"]))
        n_status, n_raw, _ = self._req(
            c, "POST", PREVIEW,
            _template(NONEXISTENT_SEASON, foreign["rink_id"]))

        report = (f"\n  foreign     -> {f_status} {f_raw}"
                  f"\n  nonexistent -> {n_status} {n_raw}")
        self.assertEqual(f_status, n_status,
                         f"foreign and nonexistent Seasons differ by status — "
                         f"the route is an existence oracle.{report}")
        # The ids themselves legitimately differ; nothing ELSE may.
        self.assertEqual(f_raw.replace(foreign["season_id"], "<SEASON>"),
                         n_raw.replace(NONEXISTENT_SEASON, "<SEASON>"),
                         f"foreign and nonexistent Seasons differ by body — "
                         f"the route is an existence oracle.{report}")

    # -- prerequisite 2: a SIBLING Season in the caller's OWN Program --------
    def test_a_sibling_season_in_my_own_program_is_refused(self):
        """The gate is the EXACT selected tuple, not "some Season I may access".

        A foreign-Program refusal is also explained by the Program ceiling, so
        it alone cannot distinguish the two rules. This one can: same Program,
        same Venue, same operator, fully authorized — and still refused,
        because it is not the Season that is SELECTED.
        """
        home, _foreign = self._two_programs("Sibling")
        sibling = self._sibling_season(home, "Sibling")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)          # ANTI-VACUITY

        before = self._slots(home["rink_id"])
        status, raw, _ = self._req(
            c, "POST", COMMIT, _template(sibling, home["rink_id"]))
        self.assertEqual(
            status, 404,
            f"a sibling Season in the caller's OWN Program was accepted — the "
            f"gate is the Program ceiling, not the selected tuple: {raw}")
        self.assertEqual(
            self._slots(home["rink_id"]), before,
            "ice was written into a Season that was not the selected one")

        # ...and it is indistinguishable from a nonexistent Season.
        n_status, n_raw, _ = self._req(
            c, "POST", COMMIT,
            _template(NONEXISTENT_SEASON, home["rink_id"]))
        self.assertEqual(status, n_status)
        self.assertEqual(raw.replace(sibling, "<S>"),
                         n_raw.replace(NONEXISTENT_SEASON, "<S>"),
                         "a sibling Season is distinguishable from a "
                         "nonexistent one — still an existence oracle")

    def test_switching_context_away_invalidates_a_valid_fingerprint(self):
        """Prerequisite 3: preview legitimately under B, switch to A, then
        replay B's commit with the STILL-VALID fingerprint.

        This is the case the fingerprint cannot cover on its own. The token is
        genuine, unexpired, and bound to this very operator — because they
        really did preview B while standing in B. Only the tuple check at
        COMMIT time can refuse it, which is why the gate has to be re-decided
        at the write and not inherited from the preview.
        """
        home, foreign = self._two_programs("Replay")
        c = self._login("arena")

        # Legitimately preview B, standing in B.
        self._use(c, foreign["program_id"], foreign["season_id"])
        status, raw, pv = self._req(
            c, "POST", PREVIEW,
            _template(foreign["season_id"], foreign["rink_id"]))
        self.assertEqual(status, 200, f"the legitimate preview failed: {raw}")
        fingerprint = pv.get("template_fingerprint")
        self.assertTrue(fingerprint, f"no fingerprint to replay: {raw}")

        # Now switch to A and replay B's commit with that valid token.
        self._use(c, home["program_id"], home["season_id"])
        before = self._slots(foreign["rink_id"])
        body = _template(foreign["season_id"], foreign["rink_id"])
        body["template_fingerprint"] = fingerprint
        status, raw, _ = self._req(c, "POST", COMMIT, body)

        self.assertEqual(
            status, 404,
            f"a fingerprint previewed under B was honoured while standing in "
            f"A — the commit inherited the preview's context: {raw}")
        self.assertEqual(
            self._slots(foreign["rink_id"]), before,
            "the replayed commit wrote ice into B while the operator stood "
            "in A")
        # ANTI-VACUITY: the token really is still good — switching BACK lets
        # the same body commit, so the refusal is the tuple and not a stale or
        # malformed fingerprint.
        self._use(c, foreign["program_id"], foreign["season_id"])
        status, raw, _ = self._req(c, "POST", COMMIT, body)
        self.assertEqual(
            status, 200,
            f"the fingerprint was rejected even in its own context, so the "
            f"refusal above proved nothing about the tuple: {raw}")
        self.assertGreater(self._slots(foreign["rink_id"]), before)

    # -- state 1: no active tuple at all ------------------------------------
    def test_no_active_context_is_a_stable_409_independent_of_the_target(self):
        """Owner ruling, state 1. With nothing selected the refusal must be a
        stable structured 409 that does NOT depend on ``season_id``.

        Otherwise the no-context path becomes the existence oracle the
        with-context path just stopped being: a caller with no selection could
        distinguish a real Season from a made-up one by the shape of the
        refusal. Compared across a REAL Season, a foreign one and a
        nonexistent one on the same route and session.
        """
        home, foreign = self._two_programs("NoCtx")
        # A BRAND-NEW operator: no persisted ActiveContext can be inherited
        # from an earlier case, so "nothing is selected" is a property of the
        # identity rather than of execution order.
        c = self._login(self._fresh_operator("noctx", Role.ARENA_MANAGER))

        seen = {}
        for label, season_id, rink_id in (
                ("real", home["season_id"], home["rink_id"]),
                ("foreign", foreign["season_id"], foreign["rink_id"]),
                ("nonexistent", NONEXISTENT_SEASON, home["rink_id"])):
            for route in (PREVIEW, COMMIT):
                status, raw, _ = self._req(
                    c, "POST", route, _template(season_id, rink_id))
                seen[(label, route)] = (
                    status, raw.replace(season_id, "<SEASON>")
                               .replace(rink_id, "<RINK>"))

        for route in (PREVIEW, COMMIT):
            statuses = {k[0]: v[0] for k, v in seen.items() if k[1] == route}
            self.assertEqual(
                set(statuses.values()), {409},
                f"{route}: no-context refusal is not a stable 409: {statuses}")
            bodies = {k[0]: v[1] for k, v in seen.items() if k[1] == route}
            self.assertEqual(
                len(set(bodies.values())), 1,
                f"{route}: the no-context refusal LEAKS which Seasons exist — "
                f"bodies differ across real/foreign/nonexistent: {bodies}")
            self.assertIn("active_context_required", bodies["real"])

    def test_no_active_context_writes_no_ice_and_no_audit(self):
        """The 409 must be a refusal, not a partial success. Zero inventory and
        zero audit side effects, on the caller's OWN Season as well as a
        foreign one — a refusal that still wrote would be worse than the
        defect."""
        home, foreign = self._two_programs("NoCtxWrite")
        c = self._login(self._fresh_operator("noctxw", Role.ARENA_MANAGER))
        before_home = self._slots(home["rink_id"])
        before_foreign = self._slots(foreign["rink_id"])
        before_audit = self._audit_count()

        for season_id, rink_id in ((home["season_id"], home["rink_id"]),
                                   (foreign["season_id"], foreign["rink_id"]),
                                   (NONEXISTENT_SEASON, home["rink_id"])):
            status, raw, _ = self._req(
                c, "POST", COMMIT, _template(season_id, rink_id))
            self.assertEqual(status, 409, raw)

        self.assertEqual(self._slots(home["rink_id"]), before_home,
                         "the no-context refusal still wrote ice")
        self.assertEqual(self._slots(foreign["rink_id"]), before_foreign,
                         "the no-context refusal still wrote FOREIGN ice")
        self.assertEqual(self._audit_count(), before_audit,
                         "the no-context refusal still wrote an audit row")

    # -- a STALE persisted selection must fail closed ------------------------
    def test_a_stale_saved_selection_fails_closed(self):
        """The PR body claims a stale selection fails closed rather than
        silently retargeting. Nothing asserted it until now.

        The danger is specific: when the saved Season can no longer be
        resolved, `ContextService._resolve_locked` falls through to
        `_fallback()`, which AUTO-SELECTS a different one. Honouring that would
        move the operator's work to a Season they never chose — silently, and
        with a 200.

        STALE means UNRESOLVABLE, which archiving does not produce: an archived
        Season stays authorized and still resolves, so the operator gets the
        more useful `season_archived` refusal naming what to reopen. That is
        correct and is asserted separately below. Here the saved Season is
        DELETED, which is what actually triggers the fallback.
        """
        home, _foreign = self._two_programs("Stale")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)               # ANTI-VACUITY

        # Make the SAVED ROW itself dangle, keeping the row present. Deleting
        # the Season instead also clears the context row, so `saved` becomes
        # None and the refusal comes from an earlier branch — the equality
        # check would never be exercised and the test would be vacuous. It
        # was, until a mutation proved it.
        store = self.srv.STATE.api.store
        saved = store.get_active_context("user_arena")
        store.set_active_context(ActiveContext(
            id=saved.id, program_id=saved.program_id,
            season_id="season_vanished", updated_at=saved.updated_at,
            league_id=saved.league_id))

        before_home = self._slots(home["rink_id"])
        before_audit = self._audit_count()
        seen = {}
        for label, target in (("the dangling selection", "season_vanished"),
                              ("what fallback would pick", home["season_id"]),
                              ("nonexistent", NONEXISTENT_SEASON)):
            status, raw, _ = self._req(
                c, "POST", COMMIT, _template(target, home["rink_id"]))
            seen[label] = (status, raw.replace(target, "<S>"))

        self.assertEqual(
            {s for s, _ in seen.values()}, {409},
            f"a stale selection did not fail closed: {seen}")
        self.assertEqual(
            len({b for _, b in seen.values()}), 1,
            f"the stale-selection refusal is target-DEPENDENT, so it leaks "
            f"which Seasons exist: {seen}")
        # The decisive one: the fallback's own pick is refused too. Without
        # the saved-vs-resolved equality check this would have committed 200.
        self.assertEqual(self._slots(home["rink_id"]), before_home,
                         "a stale selection wrote ice into the Season the "
                         "fallback silently retargeted to")
        self.assertEqual(self._audit_count(), before_audit,
                         "a stale selection still wrote an audit row")

    def test_an_archived_selection_says_so_rather_than_404ing(self):
        """The other half, and the reason the test above deletes rather than
        archives: an archived Season is still the operator's OWN valid
        selection, so flattening it into the generic 404 would hide an
        actionable message behind a privacy refusal."""
        home, _foreign = self._two_programs("Arch")
        c = self._login("arena")
        self._use(c, home["program_id"], home["season_id"])
        self._positive_control(c, home)
        self.srv.STATE.api.setup.archive_season(
            home["season_id"], reason="arch-test", actor_id="admin")

        status, raw, _ = self._req(
            c, "POST", COMMIT,
            _template(home["season_id"], home["rink_id"]))
        self.assertEqual(status, 400, raw)
        self.assertIn("season_archived", raw)

    # -- the CONTROL: the gate exists and works one route over ---------------
    def test_gated_sibling_route_refuses_the_same_foreign_season(self):
        """`/api/v2/setup/seasons/<id>/archive` names a Season exactly the way
        ice-availability does, but goes through `_guarded_mutation`. Same
        operator, same session, same active context, same foreign Season id.

        This is expected to PASS: it is the proof that the refusal the probes
        above ask for is already implemented, already conventional, and already
        byte-identical to a nonexistent id — the ice-availability route simply
        never reaches it.
        """
        home, foreign = self._two_programs("Control")
        c = self._login("admin")                 # archive needs MANAGE_SETUP
        self._use(c, home["program_id"], home["season_id"])

        f_status, f_raw, _ = self._req(
            c, "POST", f"/api/v2/setup/seasons/{foreign['season_id']}/archive", {})
        n_status, n_raw, _ = self._req(
            c, "POST", f"/api/v2/setup/seasons/{NONEXISTENT_SEASON}/archive", {})

        self.assertEqual(f_status, 404, f_raw)
        self.assertEqual(f_raw.replace(foreign["season_id"], "<SEASON>"),
                         n_raw.replace(NONEXISTENT_SEASON, "<SEASON>"),
                         f"foreign={f_raw} nonexistent={n_raw}")
        self.assertNotIn(f"{foreign['tag']} Season", f_raw)

        # And it really was refused, not merely reported as refused.
        season = self.srv.STATE.api.store.get_season(foreign["season_id"])
        self.assertEqual(season.status.value, "active", season)


if __name__ == "__main__":
    unittest.main()


# -- prerequisite 4a: TRI-STORE -------------------------------------------
def _backends():
    """Memory, SQLite and (when configured) PostgreSQL — the repo's standard
    triple.

    The scope decision reads an ActiveContext row and a Season row, so it is
    exactly the kind of rule that can pass on one store and fail on another:
    `get_active_context` is a dict lookup in Memory and a real query with real
    NULL semantics in SQL.
    """
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


class SeasonScopeRuleTriStoreTest(unittest.TestCase):
    """`setup_season_is_active` / `has_active_program_season` decide
    identically on every backend, driven at the service layer."""

    def _world(self, api):
        svc = api.setup
        pa = svc.create_program("Tri A", timezone_name=TZ)
        sa = svc.create_season(pa.id, "Tri A S1")
        sibling = svc.create_season(pa.id, "Tri A S2")
        pb = svc.create_program("Tri B", timezone_name=TZ)
        sb = svc.create_season(pb.id, "Tri B S1")
        api.accounts.create_account("triop", "demo", Role.ARENA_MANAGER)
        return pa.id, sa.id, sibling.id, sb.id

    def test_the_rule_agrees_across_backends(self):
        seen = []
        for label, store in _backends():
            seen.append(label)
            api = ApiService(store)
            pa, sa, sibling, sb = self._world(api)

            self.assertFalse(
                api.has_active_program_season(
                    "triop", Role.ARENA_MANAGER, None),
                f"[{label}] an operator who selected nothing reported an "
                f"active tuple")

            api.set_active_context("triop", Role.ARENA_MANAGER, None, pa, sa)
            self.assertTrue(
                api.has_active_program_season(
                    "triop", Role.ARENA_MANAGER, None),
                f"[{label}] an explicit selection was not recognised")

            for target, expected, why in (
                    (sa, True, "the SELECTED Season"),
                    (sibling, False, "a SIBLING Season in the same Program"),
                    (sb, False, "a FOREIGN Program's Season"),
                    ("season_does_not_exist", False, "a NONEXISTENT Season"),
                    (None, False, "no Season id at all")):
                self.assertIs(
                    api.setup_season_is_active(
                        target, "triop", Role.ARENA_MANAGER, None),
                    expected, f"[{label}] {why} decided wrongly")

            if isinstance(store, SqlStore):
                store.close()

        # ANTI-VACUITY: an empty generator would satisfy every assertion above
        # without exercising anything.
        self.assertIn("memory", seen)
        self.assertIn("sqlite", seen)
        self.assertGreaterEqual(len(seen), 2, f"backends exercised: {seen}")


# -- prerequisite 4b: the PostgreSQL context-switch RACE -------------------
@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL required (set TEST_DATABASE_URL)")
class IceCommitContextRacePgTest(unittest.TestCase):
    """The ice commit must be atomic against a CONCURRENT CONTEXT SWITCH, on
    two real PostgreSQL connections.

    The earlier revision routed this through `_guarded_mutation`, which locks
    the SEASON and resolves the tuple unlocked. Nothing contended with the
    caller's ActiveContext row, so `POST /api/context` could commit tuple B
    between the A-tuple decision and A's write and BOTH transactions
    succeeded. A Season lock cannot serialize a write to a different table.

    Proof here is not timing-based: `pg_stat_activity` is polled until the
    switching backend is genuinely `active` and waiting on `Lock`.
    """

    _TS = datetime(2027, 1, 1, tzinfo=timezone.utc)

    def setUp(self):
        self.url = os.environ["TEST_DATABASE_URL"]
        SqlStore(self.url).clear_all_data()

    def _world(self, store):
        api = ApiService(store)
        svc = api.setup
        pa = svc.create_program("Race A", timezone_name=TZ)
        sa = svc.create_season(pa.id, "Race A S1")
        va = svc.create_venue("Race A Arena", league_id=pa.id)
        svc.grant_season_venue_access(sa.id, va.id)
        ra = svc.create_rink(va.id, "Race A Sheet")
        pb = svc.create_program("Race B", timezone_name=TZ)
        sb = svc.create_season(pb.id, "Race B S1")
        api.accounts.create_account("racer", "demo", Role.ARENA_MANAGER)
        api.set_active_context("racer", Role.ARENA_MANAGER, None, pa.id, sa.id)
        return api, pa.id, sa.id, ra.id, pb.id, sb.id

    def _backend_pid(self, store):
        with store.conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid() AS pid")
            return cur.fetchone()["pid"]

    def _wait_blocked_on_lock(self, pid, timeout=10.0):
        import psycopg
        deadline = time.monotonic() + timeout
        with psycopg.connect(self.url, autocommit=True) as mon:
            while time.monotonic() < deadline:
                with mon.cursor() as cur:
                    cur.execute("SELECT state, wait_event_type FROM "
                                "pg_stat_activity WHERE pid = %s", (pid,))
                    row = cur.fetchone()
                    if row and row[0] == "active" and row[1] == "Lock":
                        return True
                time.sleep(0.02)
        return False

    def _template(self, season_id, rink_id):
        return dict(season_id=season_id, rink_ids=[rink_id], weekdays=[1, 3],
                    start_local="18:00", end_local="22:00",
                    start_date="2026-09-01", end_date="2026-09-07",
                    playable_minutes=60, turnover_minutes=15)

    def test_a_context_switch_cannot_commit_mid_flight(self):
        seed = SqlStore(self.url)
        api_seed, pa, sa, ra, pb, sb = self._world(seed)

        writer = SqlStore(self.url)      # A: commits ice into its own Season
        switcher = SqlStore(self.url)    # B: tries to switch context underneath
        api_a, api_b = ApiService(writer), ApiService(switcher)

        # A legitimate preview first: commit refuses an un-previewed template
        # (`preview_required`), and this test is about the RACE, not that gate.
        pv = api_a.preview_ice_availability(
            actor_id="racer", **self._template(sa, ra))
        fingerprint = pv.get("template_fingerprint")
        self.assertTrue(fingerprint, f"preview gave no fingerprint: {pv}")

        paused = threading.Event()
        release = threading.Event()
        orig = writer._exec

        def instrumented(query, params=()):
            result = orig(query, params)
            # Pause once the ActiveContext row is LOCKED and the tuple
            # authorized — the exact window the switch must not slip through.
            if "user_active_context" in query and "FOR UPDATE" in query.upper():
                paused.set()
                release.wait(20)
            return result
        writer._exec = instrumented

        out = {}
        def do_commit():
            out["a"] = api_a.commit_ice_availability_in_active_season(
                "racer", Role.ARENA_MANAGER, None,
                template_fingerprint=fingerprint, **self._template(sa, ra))
        def do_switch():
            out["b"] = api_b.set_active_context(
                "racer", Role.ARENA_MANAGER, None, pb, sb)

        # Capture B's backend pid BEFORE it is busy: once the switch is
        # blocked, that connection cannot answer a pid query — asking then
        # contends with the very statement being observed.
        switcher_pid = self._backend_pid(switcher)

        ta = threading.Thread(target=do_commit, daemon=True); ta.start()
        self.assertTrue(paused.wait(20), "A never reached the locked window")

        tb = threading.Thread(target=do_switch, daemon=True); tb.start()
        blocked = self._wait_blocked_on_lock(switcher_pid)
        self.assertTrue(
            blocked,
            "the context switch was NOT blocked by the commit's ActiveContext "
            "lock — a switch can land mid-flight and the commit is not atomic")

        release.set(); ta.join(20); tb.join(20)
        writer._exec = orig

        self.assertNotIn("error", out.get("a", {}), f"A failed: {out.get('a')}")
        slots = sum(1 for s in seed.all_ice_slots() if s.rink_id == ra)
        self.assertEqual(slots, 6, f"A's ice did not land: {out.get('a')}")
        for s in (seed, writer, switcher):
            s.close()

    def test_the_context_advisory_lock_is_held_during_the_unit(self):
        """An advisory lock on this user's context key is held for the whole
        commit unit, so no context write can proceed concurrently.

        KNOWN LIMIT, stated rather than papered over. This does NOT
        discriminate the SESSION-scoped `active_context_mutex` from the
        TRANSACTION-scoped `pg_advisory_xact_lock` that
        `resolve_with_league(lock=True)` takes: both use the same key
        (`_ACTIVE_CONTEXT_LOCK_NAMESPACE` + `hashtext(user_id)`), and
        `pg_locks` reports one row per key per backend, so the two grants
        collapse into a single row. Measured both ways — with and without the
        mutex — the observed count is 1.

        Removing the mutex therefore leaves this test and the race above
        GREEN. What the mutex protects is an ordering property the current
        shape does not exhibit: PostgreSQL fixes a SERIALIZABLE view at the
        FIRST query, so a unit that blocks on a lock INSIDE its transaction
        keeps the snapshot taken before the competing write committed. Here
        the lock acquisition IS the first statement, so the hazard does not
        arise and nothing observable changes.

        The mutex is retained because it is the repository's established #386
        protocol and the hazard returns the moment any query precedes it — but
        it is currently an UNFALSIFIED guard, and this docstring says so
        instead of implying coverage that does not exist.
        """
        import psycopg
        seed = SqlStore(self.url)
        api_seed, pa, sa, ra, pb, sb = self._world(seed)
        writer = SqlStore(self.url)
        api_a = ApiService(writer)
        pv = api_a.preview_ice_availability(
            actor_id="racer", **self._template(sa, ra))
        fingerprint = pv.get("template_fingerprint")

        # Scoped to THIS backend: counting advisory locks cluster-wide passes
        # on somebody else's lock, which made an earlier version of this
        # assertion survive removing the mutex entirely.
        writer_pid = self._backend_pid(writer)

        paused, release = threading.Event(), threading.Event()
        orig = writer._exec

        def instrumented(query, params=()):
            result = orig(query, params)
            if "user_active_context" in query and "FOR UPDATE" in query.upper():
                paused.set()
                release.wait(20)
            return result
        writer._exec = instrumented

        def do_commit():
            api_a.commit_ice_availability_in_active_season(
                "racer", Role.ARENA_MANAGER, None,
                template_fingerprint=fingerprint, **self._template(sa, ra))

        th = threading.Thread(target=do_commit, daemon=True); th.start()
        self.assertTrue(paused.wait(20), "never reached the locked window")
        try:
            with psycopg.connect(self.url, autocommit=True) as mon:
                with mon.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_locks WHERE locktype = "
                        "'advisory' AND granted AND pid = %s", (writer_pid,))
                    held = cur.fetchone()[0]
        finally:
            release.set(); th.join(20); writer._exec = orig

        self.assertGreaterEqual(
            held, 1,
            "no advisory lock is held during the commit unit — the per-user "
            "context mutex is not being taken, so a selection landing before "
            "BEGIN cannot be ordered against this unit")
        for s in (seed, writer):
            s.close()

    def test_a_switch_that_wins_makes_the_stale_commit_a_generic_404(self):
        """The reverse winner. B switches first, so A's target is no longer the
        selected Season: generic 404, zero ice, zero audit."""
        store = SqlStore(self.url)
        api, pa, sa, ra, pb, sb = self._world(store)
        api.set_active_context("racer", Role.ARENA_MANAGER, None, pb, sb)

        before_slots = sum(1 for s in store.all_ice_slots() if s.rink_id == ra)
        before_audit = len(store.all_setup_audit())
        result = api.commit_ice_availability_in_active_season(
            "racer", Role.ARENA_MANAGER, None,
            template_fingerprint="any-token", **self._template(sa, ra))

        self.assertIn("error", result, result)
        self.assertEqual(result["error"]["code"], "not_found", result)
        self.assertEqual(
            sum(1 for s in store.all_ice_slots() if s.rink_id == ra),
            before_slots, "a stale commit still wrote ice")
        self.assertEqual(len(store.all_setup_audit()), before_audit,
                         "a stale commit still wrote an audit row")
        store.close()

