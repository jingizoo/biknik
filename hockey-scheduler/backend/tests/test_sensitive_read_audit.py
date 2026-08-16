"""Facade sensitive-read gate + durable audit for ContactDestination (#124).

The concrete application of the privacy foundation to the one protected
surface that exists on main today: every facade read of stored contact
destinations passes the visibility policy and leaves DataAccessLog rows —
one per disclosed recipient on success, one collection-level row on refusal
or an empty registry — and the log NEVER contains a protected value
(proven by scanning every stored row against planted sentinels, with a
positive control showing the sentinels ARE where they live).

Server-level (HTTP) assertions live in test_sensitive_read_audit_http.py
(#426 review finding 1) — these tests pin the facade contract that wiring
calls.
"""

import json
import os
import tempfile
import unittest

from helpers import BACKEND, fresh_sql_store  # noqa: F401

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role
from hockey_scheduler.domain.privacy import (
    ACCESS_ALLOWED,
    ACCESS_DENIED,
    SensitiveFieldCategory,
)
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import visibility_policy as vp
from hockey_scheduler.store import SqlStore

C = SensitiveFieldCategory

# Deliberately loud, unmistakable protected values (never real PII).
SENTINEL_EMAIL = "sentinel-secret-email@leak-probe.invalid"
SENTINEL_PUSH = "sentinel-secret-push-token-XYZZY"


class _Base(unittest.TestCase):
    def setUp(self):
        self.store, self.game_id, self.ids = build_full_demo_store()
        self.api = ApiService(self.store)

    def seed_contacts(self):
        oid = self.ids["referee_id"]
        self.api.set_contact_destination(
            "official:" + oid, "email", SENTINEL_EMAIL, label="Ref")
        self.api.set_contact_destination("scheduler", "push", SENTINEL_PUSH)
        return "official:" + oid

    def rows(self, **kw):
        return self.store.list_data_access(**kw)


class AllowedReadAuditTest(_Base):
    def test_explicit_role_list_returns_rows_and_audits_each_subject(self):
        ref = self.seed_contacts()
        result = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["contacts"]), 2)

        rows = self.rows()
        self.assertEqual(len(rows), 2)  # one per DISTINCT disclosed recipient
        self.assertEqual(sorted(r.subject_id for r in rows),
                         sorted([ref, "scheduler"]))
        for r in rows:
            self.assertEqual(r.category, C.CONTACT_DESTINATION)
            self.assertEqual(r.subject_type, "recipient")
            self.assertEqual(r.purpose, "list_contact_destinations")
            self.assertEqual(r.outcome, ACCESS_ALLOWED)
            self.assertEqual(r.actor_role, "league_admin")
            self.assertEqual(r.actor_user_id, "user_admin")
            self.assertIsNotNone(r.at.tzinfo)
            self.assertTrue((r.request_id or "").startswith("req_"))
        # One read call = one correlation id across its rows.
        self.assertEqual(len({r.request_id for r in rows}), 1)
        self.assertEqual(len({r.id for r in rows}), 2)

    def test_no_principal_no_arg_call_now_fails_closed(self):
        # #426 review finding 1: the legacy no-arg "operator_boundary"
        # default-allow shape is retired. A call with no principal at all
        # now refuses exactly like any other unrecognised one, durably
        # audited as NO_PRINCIPAL.
        self.seed_contacts()
        result = self.api.list_contact_destinations()
        self.assertEqual(result["error"]["code"], "forbidden", result)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)
        self.assertIsNone(rows[0].actor_user_id)

    def test_two_reads_mint_distinct_request_ids(self):
        self.seed_contacts()
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        ids = {r.request_id for r in self.rows()}
        self.assertEqual(len(ids), 2)

    def test_caller_supplied_request_id_is_honoured(self):
        self.seed_contacts()
        result = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin",
            request_id="req_fixed_123")
        self.assertNotIn("error", result, result)
        self.assertEqual({r.request_id for r in self.rows()},
                         {"req_fixed_123"})

    def test_explicit_operator_roles_are_allowed_and_attributed(self):
        self.seed_contacts()
        res = self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertEqual(len(res["contacts"]), 2)
        # Role given as its string value works the same (the server resolves
        # strings today).
        res = self.api.list_contact_destinations(
            actor_role="arena_manager", actor_user_id="user_arena")
        self.assertEqual(len(res["contacts"]), 2)

        by_actor = {}
        for r in self.rows():
            by_actor.setdefault((r.actor_user_id, r.actor_role), []).append(r)
        self.assertEqual(len(by_actor[("user_admin", "league_admin")]), 2)
        self.assertEqual(len(by_actor[("user_arena", "arena_manager")]), 2)

    def test_empty_registry_read_still_leaves_a_trace(self):
        result = self.api.list_contact_destinations(
            actor_role=Role.ARENA_MANAGER)
        self.assertEqual(result["contacts"], [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].subject_type, rows[0].subject_id),
                         ("recipient", "*"))
        self.assertEqual(rows[0].outcome, ACCESS_ALLOWED)

    def test_list_read_does_not_touch_the_mutation_audit_trails(self):
        self.seed_contacts()
        setup_audits = len(self.store.all_setup_audit())
        roster_audits = len(self.store.audit)
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(len(self.store.all_setup_audit()), setup_audits)
        self.assertEqual(len(self.store.audit), roster_audits)


class FailClosedRefusalTest(_Base):
    UNAUTHORIZED = (Role.COACH, Role.VIEWER, Role.PLAYER, Role.GUARDIAN,
                    Role.OFFICIAL)

    def test_coach_gets_refusal_and_a_denial_row_and_no_values(self):
        self.seed_contacts()
        result = self.api.list_contact_destinations(
            actor_role=Role.COACH, actor_user_id="user_coach")
        self.assertEqual(result["error"]["code"], "forbidden")
        details = result["error"]["details"]
        self.assertEqual(details["reason"], "sensitive_read_denied")
        self.assertEqual(details["category"], "contact_destination")
        self.assertTrue(details["request_id"].startswith("req_"))

        # Payload omission: no contacts key, and neither sentinel value
        # appears anywhere in the serialized refusal.
        payload = json.dumps(result)
        self.assertNotIn("contacts", result)
        self.assertNotIn(SENTINEL_EMAIL, payload)
        self.assertNotIn(SENTINEL_PUSH, payload)

        rows = self.rows()
        self.assertEqual(len(rows), 1)  # the denial row and nothing else
        denial = rows[0]
        self.assertEqual(denial.outcome, ACCESS_DENIED)
        self.assertEqual(denial.actor_role, "coach")
        self.assertEqual(denial.actor_user_id, "user_coach")
        self.assertEqual((denial.subject_type, denial.subject_id),
                         ("recipient", "*"))
        self.assertEqual(denial.purpose, "list_contact_destinations")
        self.assertEqual(denial.request_id, details["request_id"])

    def test_every_unauthorized_role_is_refused_with_a_denial_row(self):
        self.seed_contacts()
        for n, role in enumerate(self.UNAUTHORIZED, start=1):
            result = self.api.list_contact_destinations(
                actor_role=role, actor_user_id=f"user_{role.value}")
            self.assertEqual(result["error"]["code"], "forbidden", role)
            rows = self.rows()
            self.assertEqual(len(rows), n, role)
            self.assertEqual(rows[-1].outcome, ACCESS_DENIED, role)
            self.assertEqual(rows[-1].actor_role, role.value, role)

    def test_public_or_unknown_claims_fail_closed_and_are_recorded(self):
        self.seed_contacts()
        for claim in ("public", "root", ""):
            result = self.api.list_contact_destinations(actor_role=claim)
            self.assertEqual(result["error"]["code"], "forbidden", claim)
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        # The unparseable claim is not copied into the log verbatim.
        self.assertEqual({r.actor_role for r in rows}, {"unknown"})
        self.assertEqual({r.outcome for r in rows}, {ACCESS_DENIED})

    def test_denial_enumerates_no_subjects(self):
        # A refused caller learned nothing — so the log must not enumerate
        # the recipients that WOULD have been disclosed.
        ref = self.seed_contacts()
        self.api.list_contact_destinations(actor_role=Role.VIEWER)
        subjects = {r.subject_id for r in self.rows()}
        self.assertEqual(subjects, {"*"})
        self.assertNotIn(ref, subjects)

    def test_denial_row_survives_although_the_call_returned_an_error(self):
        # The denial is committed BEFORE the refusal is raised; @catch turns
        # the raise into the error dict, and the row must still be there —
        # including across a transaction the caller opened afterwards.
        self.seed_contacts()
        self.api.list_contact_destinations(actor_role=Role.COACH)
        self.assertEqual(len(self.rows()), 1)
        with self.store.transaction():
            pass  # an unrelated committed transaction doesn't disturb it
        self.assertEqual(len(self.rows()), 1)


class ValueNeverInLogTest(_Base):
    def test_no_stored_row_field_contains_a_protected_value(self):
        ref = self.seed_contacts()
        # Exercise every emitting path: allowed list, refused list
        # (explicit unauthorized role AND no principal at all), allowed
        # read-back toggle.
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        self.api.list_contact_destinations(actor_role=Role.COACH)
        self.api.list_contact_destinations()
        cid = next(c.id for c in self.store.all_contact_destinations()
                   if c.recipient_ref == ref)
        self.api.set_contact_destination_active(
            cid, False, actor_id="user_1", actor_role=Role.ARENA_MANAGER)

        rows = self.rows()
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            for field_name, value in vars(row).items():
                text = str(value)
                self.assertNotIn(SENTINEL_EMAIL, text,
                                 f"{field_name} leaked the email value")
                self.assertNotIn(SENTINEL_PUSH, text,
                                 f"{field_name} leaked the push value")

        # POSITIVE CONTROL: the probe finds the sentinels where they DO live,
        # so an accidental copy into the log could not hide from it.
        stored = [c.destination for c in self.store.all_contact_destinations()]
        self.assertIn(SENTINEL_EMAIL, stored)
        self.assertIn(SENTINEL_PUSH, stored)


class RequestIdSanitizationTest(_Base):
    """#426 review finding 3: "request_id is honored verbatim; passing the
    planted email as request_id stored that email in
    DataAccessLog.request_id" — exact sentinel-through-persisted-column
    proofs for ``safe_request_id`` (services/visibility_policy.py), on
    both the ALLOWED and DENIED emission paths, plus the "one request
    shares one id, distinct requests differ" contract the field-name-only
    schema test alone could not have caught.
    """

    MALFORMED_REQUEST_IDS = (
        SENTINEL_EMAIL,                       # an email planted as an id
        SENTINEL_PUSH,                        # a token planted as an id
        "req_line1\nline2",                   # embedded newline
        "req_" + "a" * 200,                   # oversized (limit is 80)
        "",                                   # empty
        "not-even-req-shaped",                # missing the req_ prefix
    )

    def test_sentinel_request_id_never_persisted_on_an_allowed_read(self):
        self.seed_contacts()
        self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, request_id=SENTINEL_EMAIL)
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            self.assertNotEqual(row.request_id, SENTINEL_EMAIL)
            self.assertTrue(row.request_id.startswith("req_"), row.request_id)

    def test_sentinel_request_id_never_persisted_on_a_denied_read(self):
        self.seed_contacts()
        self.api.list_contact_destinations(
            actor_role=Role.COACH, request_id=SENTINEL_PUSH)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0].request_id, SENTINEL_PUSH)
        self.assertTrue(rows[0].request_id.startswith("req_"))

    def test_every_malformed_request_id_shape_is_replaced_not_persisted(self):
        for planted in self.MALFORMED_REQUEST_IDS:
            with self.subTest(planted=repr(planted)[:40]):
                store, _gid, ids = build_full_demo_store()
                api = ApiService(store)
                oid = ids["referee_id"]
                api.set_contact_destination(
                    "official:" + oid, "email", SENTINEL_EMAIL)
                api.list_contact_destinations(
                    actor_role=Role.LEAGUE_ADMIN, request_id=planted)
                for row in store.list_data_access():
                    self.assertNotEqual(row.request_id, planted)
                    if row.request_id is not None:
                        self.assertRegex(row.request_id, r"^req_[A-Za-z0-9_-]{1,80}$")

    def test_a_validly_shaped_caller_supplied_request_id_IS_honored(self):
        # The sanitizer is not a blanket "always mint fresh" — a value that
        # already looks like this module's own opaque shape passes through,
        # which is what lets ONE real request thread ONE id across several
        # DataAccessLog rows (see the next test).
        self.seed_contacts()
        valid_id = "req_" + "a" * 20
        self.api.list_contact_destinations(
            actor_role=Role.LEAGUE_ADMIN, request_id=valid_id)
        rows = self.rows()
        self.assertTrue(rows, "expected at least one row")
        for row in rows:
            self.assertEqual(row.request_id, valid_id)

    def test_one_request_shares_one_id_distinct_requests_differ(self):
        self.seed_contacts()
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        self.api.list_contact_destinations(actor_role=Role.ARENA_MANAGER)
        rows = self.rows()
        by_request = {}
        for row in rows:
            by_request.setdefault(row.request_id, set()).add(row.actor_role)
        # Two distinct top-level calls -> two distinct request ids, and
        # neither id is shared across the two different calls' rows.
        self.assertEqual(len(by_request), 2, rows)
        for actors in by_request.values():
            self.assertEqual(len(actors), 1)


class SubjectIdSanitizationTest(_Base):
    """#426 review finding 3: "Arbitrary legacy recipient_ref is likewise
    copied verbatim into subject_id; setting recipient and destination to
    the same sentinel persisted it there" — exact sentinel-through-
    persisted-column proof for ``canonical_subject_id``. A bare (non
    ``player:``/``official:``-prefixed) ``recipient_ref`` is not rejected
    by ``_reject_dangling_recipient`` (see its own docstring: only those
    two structured prefixes are validated), so an arbitrary legacy value —
    exactly the shape the review demonstrated — reaches this boundary
    unfiltered from the write side.
    """

    def test_arbitrary_recipient_ref_subject_id_is_never_the_raw_value(self):
        # SENTINEL_EMAIL as the recipient_ref itself (not just the
        # destination) -- the review's own repro shape.
        self.api.set_contact_destination(
            SENTINEL_EMAIL, "email", "unrelated@example.invalid")
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        rows = self.rows()
        self.assertTrue(rows, "expected at least one row")
        for row in rows:
            self.assertNotEqual(row.subject_id, SENTINEL_EMAIL)
            self.assertNotIn(SENTINEL_EMAIL, row.subject_id)
        # Deterministic: the SAME unsafe input pseudonymises to the SAME
        # opaque id every time, so repeat reads of the same subject still
        # group together in a "who read this person's data" query.
        self.api.list_contact_destinations(actor_role=Role.ARENA_MANAGER)
        opaque_ids = {r.subject_id for r in self.rows()
                     if r.purpose == "list_contact_destinations"}
        matching = [i for i in opaque_ids if i.startswith("opaque:")]
        self.assertEqual(len(matching), 1, opaque_ids)

    def test_normal_recipient_ref_shape_passes_through_unchanged(self):
        # Contrast case: a well-formed ref is NOT obscured -- the
        # sanitizer only intervenes on what does not already look safe.
        ref = self.seed_contacts()
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        rows = [r for r in self.rows() if r.subject_id == ref]
        self.assertTrue(rows, self.rows())


class ReadBackToggleAuditTest(_Base):
    def _seed_official_contact(self):
        ref = self.seed_contacts()
        return ref, next(c.id for c in self.store.all_contact_destinations()
                         if c.recipient_ref == ref)

    def test_toggle_readback_is_audited_with_the_actor(self):
        ref, cid = self._seed_official_contact()
        result = self.api.set_contact_destination_active(
            cid, False, actor_id="user_ops", actor_role=Role.ARENA_MANAGER)
        self.assertNotIn("error", result, result)
        rows = self.rows(subject_id=ref)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.purpose, "set_contact_destination_active")
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_user_id, "user_ops")
        self.assertEqual(row.actor_role, "arena_manager")

    def test_toggle_with_no_principal_now_fails_closed(self):
        # #426 review finding 1: no more no-arg default-allow.
        ref, cid = self._seed_official_contact()
        result = self.api.set_contact_destination_active(
            cid, False, actor_id="user_ops")
        self.assertEqual(result["error"]["code"], "forbidden", result)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)
        c = next(c for c in self.store.all_contact_destinations()
                if c.id == cid)
        self.assertTrue(c.active)  # untouched

    def test_toggle_refused_for_unauthorized_role_before_any_mutation(self):
        ref, cid = self._seed_official_contact()
        result = self.api.set_contact_destination_active(
            cid, False, actor_id="user_coach", actor_role=Role.COACH)
        self.assertEqual(result["error"]["code"], "forbidden")
        # Fail-closed ordering: refused BEFORE the lookup/mutation — the row
        # is untouched and still active.
        c = next(c for c in self.store.all_contact_destinations()
                 if c.id == cid)
        self.assertTrue(c.active)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].subject_id, "*")

    def test_unauthorized_caller_cannot_probe_row_existence(self):
        # Same refusal for an id that does not exist — the privacy gate runs
        # first, so forbidden vs not_found never leaks existence.
        self._seed_official_contact()
        result = self.api.set_contact_destination_active(
            "contact_missing", False, actor_role=Role.COACH)
        self.assertEqual(result["error"]["code"], "forbidden")

    def test_not_found_toggle_leaves_no_read_row(self):
        result = self.api.set_contact_destination_active(
            "contact_missing", False, actor_id="user_ops",
            actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(self.rows(), [])

    def test_ineligible_recipient_toggle_leaves_no_read_row(self):
        # A team-scoped row is refused by the existing lifecycle validation;
        # its stored destination is never echoed, so no read row either.
        self.api.set_contact_destination("team:team_1", "email",
                                         "team@x.invalid")
        cid = next(c.id for c in self.store.all_contact_destinations()
                   if c.recipient_ref == "team:team_1")
        result = self.api.set_contact_destination_active(
            cid, False, actor_id="user_ops", actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(self.rows(), [])

    def test_upsert_write_is_not_treated_as_a_sensitive_read(self):
        # set_contact_destination's response echoes only the caller's own
        # submitted values — no stored value is disclosed, so no read row.
        self.api.set_contact_destination("scheduler", "email",
                                         "ops@contacts.invalid")
        self.assertEqual(self.rows(), [])


class SqliteFacadeDurabilityTest(unittest.TestCase):
    """The full path through a REAL database: facade reads emit rows that
    survive closing and reopening the store — durable auditing, not
    in-process bookkeeping."""

    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        self._reopen = lambda: SqlStore(self._tmp)
        return SqlStore(self._tmp)

    def test_allowed_and_denied_rows_survive_reopen(self):
        store = self._store()
        api = ApiService(store)
        api.set_contact_destination("scheduler", "email", SENTINEL_EMAIL)
        self.assertNotIn(
            "error",
            api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN),
            "allowed read")
        refusal = api.list_contact_destinations(
            actor_role=Role.COACH, actor_user_id="user_coach")
        self.assertEqual(refusal["error"]["code"], "forbidden")
        store.close()

        reopened = self._reopen()
        self.addCleanup(reopened.close)
        rows = reopened.list_data_access()
        self.assertEqual(len(rows), 2)
        outcomes = {r.outcome for r in rows}
        self.assertEqual(outcomes, {ACCESS_ALLOWED, ACCESS_DENIED})
        for row in rows:
            self.assertNotIn(SENTINEL_EMAIL, str(vars(row)))
        # Positive control on the reopened store, same as the memory probe.
        self.assertIn(SENTINEL_EMAIL,
                      [c.destination
                       for c in reopened.all_contact_destinations()])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresFacadeDurabilityTest(unittest.TestCase):
    def test_allowed_and_denied_rows_survive_reopen(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = fresh_sql_store(url)
        api = ApiService(store)
        api.set_contact_destination("scheduler", "email", SENTINEL_EMAIL)
        self.assertNotIn(
            "error",
            api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN),
            "allowed read")
        refusal = api.list_contact_destinations(actor_role=Role.COACH)
        self.assertEqual(refusal["error"]["code"], "forbidden")
        store.close()

        reopened = SqlStore(url)
        self.addCleanup(reopened.close)
        rows = reopened.list_data_access()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.outcome for r in rows},
                         {ACCESS_ALLOWED, ACCESS_DENIED})
        for row in rows:
            self.assertNotIn(SENTINEL_EMAIL, str(vars(row)))


if __name__ == "__main__":
    unittest.main()
