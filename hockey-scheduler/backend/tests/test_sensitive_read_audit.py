"""Facade sensitive-read gate + durable audit for ContactDestination (#124)
and DeviceToken (#426 round-3 review finding 1).

The concrete application of the privacy foundation to the protected
surfaces that exist on main today: every facade read of stored contact
destinations OR device tokens passes the visibility policy and leaves
DataAccessLog rows — one per disclosed recipient on success, one
collection-level row on refusal or an empty registry — and the log NEVER
contains a protected value (proven by scanning every stored row against
planted sentinels, with a positive control showing the sentinels ARE
where they live).

DeviceToken shares the EXACT SAME CONTACT_DESTINATION category/gate
ContactDestination uses (#426 round-3 review finding 1: "one
CONTACT_DESTINATION-category gate that both ... route through, not two
similar-but-separate gates") — so the ContactDestination classes above
already prove the shared sanitizer/durability machinery
(safe_request_id/canonical_subject_id, durable-denial-survives-rollback
id/seq allocation) works; the DeviceToken classes below focus on what is
actually NEW: gating list_device_tokens/set_device_token_active
specifically, not re-proving the shared machinery a second time.

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
from hockey_scheduler.domain import Position, Program, Role, Team
from hockey_scheduler.domain.privacy import (
    ACCESS_ALLOWED,
    ACCESS_DENIED,
    SensitiveFieldCategory,
)
from hockey_scheduler.full_demo import build_full_demo_store
from hockey_scheduler.services import visibility_policy as vp
from hockey_scheduler.store import InMemoryStore, SqlStore

C = SensitiveFieldCategory

# Deliberately loud, unmistakable protected values (never real PII).
SENTINEL_EMAIL = "sentinel-secret-email@leak-probe.invalid"
SENTINEL_PUSH = "sentinel-secret-push-token-XYZZY"
# Distinct from SENTINEL_PUSH above, which is a ContactDestination row
# (channel=push) — this one plants a REAL DeviceToken row (#426 round-3
# review finding 1's own repro shape: "push-secret-token-426").
SENTINEL_DEVICE_TOKEN = "sentinel-secret-device-token-QWERTY"
# #424 audit-wiring: BIRTHDATE/REGISTRATION_NUMBER sentinels for the three
# newly-gated call sites below (list_players include_identity,
# player_duplicate_report, evaluate_player_eligibility).
SENTINEL_BIRTHDATE = "2015-03-02"
SENTINEL_REGISTRATION = "sentinel-secret-registration-QWERTY"


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

    def seed_device_token(self):
        oid = self.ids["referee_id"]
        row = self.api.register_device_token(
            "official:" + oid, "fcm", SENTINEL_DEVICE_TOKEN, label="Ref phone")
        return "official:" + oid, row["id"]

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
        dt_ref, dt_id = self.seed_device_token()
        # Exercise every emitting path: allowed list, refused list
        # (explicit unauthorized role AND no principal at all), allowed
        # read-back toggle — for BOTH ContactDestination and DeviceToken
        # (#426 round-3 review finding 1: same category, same log, so a
        # planted device-token sentinel must be just as absent).
        self.api.list_contact_destinations(actor_role=Role.LEAGUE_ADMIN)
        self.api.list_contact_destinations(actor_role=Role.COACH)
        self.api.list_contact_destinations()
        self.api.list_device_tokens(actor_role=Role.LEAGUE_ADMIN)
        self.api.list_device_tokens(actor_role=Role.COACH)
        self.api.list_device_tokens()
        cid = next(c.id for c in self.store.all_contact_destinations()
                   if c.recipient_ref == ref)
        self.api.set_contact_destination_active(
            cid, False, actor_id="user_1", actor_role=Role.ARENA_MANAGER)
        self.api.set_device_token_active(
            dt_id, False, actor_id="user_1", actor_role=Role.ARENA_MANAGER)

        rows = self.rows()
        self.assertGreaterEqual(len(rows), 8)
        for row in rows:
            for field_name, value in vars(row).items():
                text = str(value)
                self.assertNotIn(SENTINEL_EMAIL, text,
                                 f"{field_name} leaked the email value")
                self.assertNotIn(SENTINEL_PUSH, text,
                                 f"{field_name} leaked the push contact value")
                self.assertNotIn(SENTINEL_DEVICE_TOKEN, text,
                                 f"{field_name} leaked the device token value")

        # POSITIVE CONTROL: the probe finds the sentinels where they DO live,
        # so an accidental copy into the log could not hide from it.
        stored = [c.destination for c in self.store.all_contact_destinations()]
        self.assertIn(SENTINEL_EMAIL, stored)
        self.assertIn(SENTINEL_PUSH, stored)
        stored_tokens = [t.token for t in self.store.all_device_tokens()]
        self.assertIn(SENTINEL_DEVICE_TOKEN, stored_tokens)


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


# -- DeviceToken (#426 round-3 review finding 1) -----------------------------
#
# DeviceToken shares the EXACT SAME CONTACT_DESTINATION category/gate the
# ContactDestination classes above already exercise — the three classes
# below therefore focus on what changed (list_device_tokens/
# set_device_token_active themselves), not on re-proving the SHARED
# sanitizer/durability machinery those ContactDestination classes already
# cover for both surfaces at once (safe_request_id/canonical_subject_id
# and durable-denial-survives-rollback id/seq allocation apply identically
# here, since it is the SAME _record_sensitive_read/_refuse_sensitive_read
# code path, not a second copy).

class DeviceTokenListAuditTest(_Base):
    def test_allowed_list_returns_rows_and_audits_the_subject(self):
        ref, _tok_id = self.seed_device_token()
        result = self.api.list_device_tokens(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["device_tokens"]), 1)
        self.assertEqual(result["device_tokens"][0]["token"],
                         SENTINEL_DEVICE_TOKEN)

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.category, C.CONTACT_DESTINATION)
        self.assertEqual(row.subject_type, "recipient")
        self.assertEqual(row.subject_id, ref)
        self.assertEqual(row.purpose, "list_device_tokens")
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_role, "league_admin")
        self.assertEqual(row.actor_user_id, "user_admin")
        self.assertTrue(row.request_id.startswith("req_"))

    def test_no_principal_no_arg_call_fails_closed(self):
        self.seed_device_token()
        result = self.api.list_device_tokens()
        self.assertEqual(result["error"]["code"], "forbidden", result)
        self.assertNotIn("device_tokens", result)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)

    def test_every_unauthorized_role_is_refused_with_a_denial_row(self):
        self.seed_device_token()
        unauthorized = (Role.COACH, Role.VIEWER, Role.PLAYER, Role.GUARDIAN,
                        Role.OFFICIAL)
        for n, role in enumerate(unauthorized, start=1):
            result = self.api.list_device_tokens(
                actor_role=role, actor_user_id=f"user_{role.value}")
            self.assertEqual(result["error"]["code"], "forbidden", role)
            self.assertNotIn(SENTINEL_DEVICE_TOKEN, json.dumps(result))
            rows = self.rows()
            self.assertEqual(len(rows), n, role)
            self.assertEqual(rows[-1].outcome, ACCESS_DENIED, role)
            self.assertEqual(rows[-1].actor_role, role.value, role)
            self.assertEqual(rows[-1].subject_id, "*", role)

    def test_empty_registry_read_still_leaves_a_trace(self):
        result = self.api.list_device_tokens(actor_role=Role.ARENA_MANAGER)
        self.assertEqual(result["device_tokens"], [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].subject_type, rows[0].subject_id),
                         ("recipient", "*"))
        self.assertEqual(rows[0].outcome, ACCESS_ALLOWED)


class DeviceTokenReadBackToggleAuditTest(_Base):
    def test_toggle_readback_is_audited_with_the_actor(self):
        ref, tok_id = self.seed_device_token()
        result = self.api.set_device_token_active(
            tok_id, False, actor_id="user_ops", actor_role=Role.ARENA_MANAGER)
        self.assertNotIn("error", result, result)
        self.assertEqual(result["token"], SENTINEL_DEVICE_TOKEN)
        rows = self.rows(subject_id=ref)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.purpose, "set_device_token_active")
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_user_id, "user_ops")
        self.assertEqual(row.actor_role, "arena_manager")

    def test_toggle_with_no_principal_fails_closed(self):
        ref, tok_id = self.seed_device_token()
        result = self.api.set_device_token_active(tok_id, False,
                                                   actor_id="user_ops")
        self.assertEqual(result["error"]["code"], "forbidden", result)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)
        t = self.store.get_device_token(tok_id)
        self.assertTrue(t.active)  # untouched

    def test_toggle_refused_for_unauthorized_role_before_any_mutation(self):
        ref, tok_id = self.seed_device_token()
        result = self.api.set_device_token_active(
            tok_id, False, actor_id="user_coach", actor_role=Role.COACH)
        self.assertEqual(result["error"]["code"], "forbidden")
        t = self.store.get_device_token(tok_id)
        self.assertTrue(t.active)  # fail-closed BEFORE the lookup/mutation
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(rows[0].subject_id, "*")

    def test_unauthorized_caller_cannot_probe_row_existence(self):
        # Same refusal for an id that does not exist — the privacy gate
        # runs first, so forbidden vs not_found never leaks existence.
        self.seed_device_token()
        result = self.api.set_device_token_active(
            "devtok_missing", False, actor_role=Role.COACH)
        self.assertEqual(result["error"]["code"], "forbidden")

    def test_not_found_toggle_leaves_no_read_row(self):
        result = self.api.set_device_token_active(
            "devtok_missing", False, actor_id="user_ops",
            actor_role=Role.LEAGUE_ADMIN)
        self.assertEqual(result["error"]["code"], "not_found")
        self.assertEqual(self.rows(), [])

    def test_register_write_is_not_treated_as_a_sensitive_read(self):
        # register_device_token's response echoes only the caller's own
        # submitted token — no stored value is disclosed, so no read row,
        # on both the create AND the reactivate-existing path.
        self.api.register_device_token("scheduler", "fcm", "tok-own-value")
        self.api.register_device_token(
            "scheduler", "fcm", "tok-own-value", label="renamed")
        self.assertEqual(self.rows(), [])


class SqliteDeviceTokenFacadeDurabilityTest(unittest.TestCase):
    """The full path through a REAL database: facade reads emit rows that
    survive closing and reopening the store — durable auditing, not
    in-process bookkeeping (mirrors SqliteFacadeDurabilityTest, DeviceToken
    side, #426 round-3 review finding 1)."""

    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        self._reopen = lambda: SqlStore(self._tmp)
        return SqlStore(self._tmp)

    def test_allowed_and_denied_rows_survive_reopen(self):
        store = self._store()
        self.addCleanup(store.close)
        api = ApiService(store)
        api.register_device_token("scheduler", "fcm", SENTINEL_DEVICE_TOKEN)
        self.assertNotIn(
            "error",
            api.list_device_tokens(actor_role=Role.LEAGUE_ADMIN),
            "allowed read")
        refusal = api.list_device_tokens(
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
            self.assertNotIn(SENTINEL_DEVICE_TOKEN, str(vars(row)))
        # Positive control on the reopened store, same as the memory probe.
        self.assertIn(SENTINEL_DEVICE_TOKEN,
                      [t.token for t in reopened.all_device_tokens()])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresDeviceTokenFacadeDurabilityTest(unittest.TestCase):
    def test_allowed_and_denied_rows_survive_reopen(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = fresh_sql_store(url)
        self.addCleanup(store.close)
        api = ApiService(store)
        api.register_device_token("scheduler", "fcm", SENTINEL_DEVICE_TOKEN)
        self.assertNotIn(
            "error",
            api.list_device_tokens(actor_role=Role.LEAGUE_ADMIN),
            "allowed read")
        refusal = api.list_device_tokens(actor_role=Role.COACH)
        self.assertEqual(refusal["error"]["code"], "forbidden")
        store.close()

        reopened = SqlStore(url)
        self.addCleanup(reopened.close)
        rows = reopened.list_data_access()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.outcome for r in rows},
                         {ACCESS_ALLOWED, ACCESS_DENIED})
        for row in rows:
            self.assertNotIn(SENTINEL_DEVICE_TOKEN, str(vars(row)))


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


class _IdentityAuditBase(unittest.TestCase):
    """A MINIMAL, isolated fixture (its own Program/Season/League/
    LeagueSeason/Division/Team, not the noisy pilot-scale full_demo_store
    the classes above use) so ALLOW/DENY row counts are exact and never
    entangled with the demo's own dozens of pre-seeded players or with
    #369 Program-scoping for a role that has no assignment into the demo's
    Program. See test_athlete_identity.py's FacadePrivacyTest and
    test_age_eligibility.py's EligibilityRuleServiceTest for the same
    fixture shape used the same way."""

    def setUp(self):
        from datetime import datetime, timezone
        from hockey_scheduler.domain import (
            Division, League, LeagueSeason, Season)
        self.store = InMemoryStore()
        self.store.add_program(Program(id="pr", name="P"))
        self.store.add_season(Season(
            id="se", program_id="pr", name="Fall",
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        self.store.add_league(League(id="lg", program_id="pr", name="L"))
        self.store.add_league_season(
            LeagueSeason(id="ls", league_id="lg", season_id="se"))
        self.store.add_division(Division(
            id="d", league_season_id="ls", name="D1", age_group="U10"))
        self.store.add_team(Team(id="t1", name="T1", program_id="pr"))
        self.store.add_team(Team(id="t2", name="T2", program_id="pr"))
        self.api = ApiService(self.store)

    def rows(self, **kw):
        return self.store.list_data_access(**kw)


# =========================================================================== #
# #424 audit-wiring: list_players(include_identity=True) — BIRTHDATE and      #
# REGISTRATION_NUMBER, gated as two INDEPENDENT categories.                   #
# =========================================================================== #
class IdentityFieldReadAuditTest(_IdentityAuditBase):
    """Mirrors AllowedReadAuditTest/FailClosedRefusalTest/ValueNeverInLogTest
    above, but for ``list_players(include_identity=True)`` — the LIST
    itself is never gated (mask, not refuse), matching ``include_email``'s
    own precedent two branches below it in ``ApiService.list_players``."""

    def _seed(self):
        return self.api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Priv",
            last_name="Fields", birthdate=SENTINEL_BIRTHDATE,
            registration_number=SENTINEL_REGISTRATION)

    def test_authorized_role_gets_both_fields_and_one_allowed_row_per_category(self):
        p = self._seed()
        rows_out = self.api.list_players(
            team_id="t1", include_identity=True, role=Role.LEAGUE_ADMIN,
            user_id="user_admin")
        self.assertEqual(len(rows_out), 1)
        row = rows_out[0]
        self.assertEqual(row["birthdate"], SENTINEL_BIRTHDATE)
        self.assertEqual(row["registration_number"], SENTINEL_REGISTRATION)

        rows = self.rows()
        self.assertEqual(len(rows), 2)  # one per category, per #424 design
        self.assertEqual({r.category for r in rows},
                         {C.BIRTHDATE, C.REGISTRATION_NUMBER})
        for r in rows:
            self.assertEqual(r.outcome, ACCESS_ALLOWED)
            self.assertEqual(r.actor_role, "league_admin")
            self.assertEqual(r.actor_user_id, "user_admin")
            self.assertEqual(r.subject_id, f"player:{p.id}")
            self.assertEqual(r.purpose, "list_players_identity")
        # One call = one correlation id across both category rows.
        self.assertEqual(len({r.request_id for r in rows}), 1)

    def test_unauthorized_but_scoped_role_masks_both_fields_and_records_denials(self):
        # VIEWER is a #369 GLOBAL scoping role (so the roster itself is NOT
        # hidden by Program-scoping) but has NO privacy grant for either
        # category (see visibility_policy._POLICY) — isolating the
        # assertion to the IDENTITY gate specifically, not scoping.
        self._seed()
        rows_out = self.api.list_players(
            team_id="t1", include_identity=True, role=Role.VIEWER,
            user_id="user_viewer")
        self.assertEqual(len(rows_out), 1)
        row = rows_out[0]
        self.assertIsNone(row["birthdate"])
        self.assertIsNone(row["registration_number"])

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.category for r in rows},
                         {C.BIRTHDATE, C.REGISTRATION_NUMBER})
        for r in rows:
            self.assertEqual(r.outcome, ACCESS_DENIED)
            self.assertEqual((r.subject_type, r.subject_id),
                             ("recipient", "*"))

    def test_no_principal_masks_and_records_denials(self):
        self._seed()
        rows_out = self.api.list_players(team_id="t1",
                                         include_identity=True)
        for row in rows_out:
            self.assertIsNone(row.get("birthdate"))
            self.assertIsNone(row.get("registration_number"))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.actor_role for r in rows}, {vp.NO_PRINCIPAL})

    def test_include_identity_false_gates_nothing_and_audits_nothing(self):
        self._seed()
        self.api.list_players(team_id="t1")
        self.assertEqual(len(self.rows()), 0)

    def test_no_stored_row_contains_the_sentinel_values(self):
        self._seed()
        self.api.list_players(team_id="t1", include_identity=True,
                              role=Role.LEAGUE_ADMIN)
        self.api.list_players(team_id="t1", include_identity=True,
                              role=Role.VIEWER)
        self.api.list_players(team_id="t1", include_identity=True)
        for row in self.rows():
            text = str(vars(row))
            self.assertNotIn(SENTINEL_BIRTHDATE, text)
            self.assertNotIn(SENTINEL_REGISTRATION, text)
        # Positive control: the sentinels DO live on the stored Player.
        players = self.store.players_for_team("t1")
        self.assertIn(SENTINEL_BIRTHDATE, [p.birthdate for p in players])
        self.assertIn(SENTINEL_REGISTRATION,
                      [p.registration_number for p in players])


# =========================================================================== #
# #424 audit-wiring: player_duplicate_report — REGISTRATION_NUMBER,           #
# whole-call refusal (no partial/masked report).                             #
# =========================================================================== #
class PlayerDuplicateReportAuditTest(_IdentityAuditBase):
    def _seed_pair(self):
        p1 = self.api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Sam", last_name="Lee",
            registration_number=SENTINEL_REGISTRATION)
        p2 = self.api.setup.add_player(
            "t2", None, Position.GOALIE, first_name="Alex", last_name="Wu",
            registration_number=SENTINEL_REGISTRATION)
        return p1, p2

    def test_authorized_role_gets_the_report_and_one_allowed_row_per_disclosed_player(self):
        p1, p2 = self._seed_pair()
        report = self.api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", report)
        shared = [w for w in report["warnings"]
                 if w["type"] == "shared_registration_number"]
        self.assertEqual(shared[0]["registration_number"],
                         SENTINEL_REGISTRATION)

        # #424 round-N+1 owner review finding A: audit subjects are sourced
        # from the FULL player collection the underlying service examined
        # (here, both seeded players), never inferred from which players
        # happen to be implicated in an emitted warning. Both p1 and p2 are
        # examined for BOTH categories -- p1/p2's registration_number is
        # what produced the one warning, and their birthdate is read (and
        # found None) by the very same by_name_birthdate pass that would
        # have raised a same_name_same_birthdate warning had it matched.
        # One ALLOWED row per (player, category): 2 players x 2 categories.
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        reg_rows = [r for r in rows if r.category == C.REGISTRATION_NUMBER]
        bd_rows = [r for r in rows if r.category == C.BIRTHDATE]
        self.assertEqual({r.subject_id for r in reg_rows},
                         {f"player:{p1.id}", f"player:{p2.id}"})
        self.assertEqual({r.subject_id for r in bd_rows},
                         {f"player:{p1.id}", f"player:{p2.id}"})
        for row in rows:
            self.assertIn(row.category, (C.REGISTRATION_NUMBER, C.BIRTHDATE))
            self.assertEqual(row.outcome, ACCESS_ALLOWED)
            self.assertEqual(row.actor_role, "league_admin")
            self.assertEqual(row.actor_user_id, "user_admin")
            self.assertEqual(row.purpose, "player_duplicate_report")
        self.assertEqual(len({r.request_id for r in rows}), 1)

    def test_unrelated_examined_players_are_audited_too(self):
        # #424 round-N+1 owner review finding A -- THE regression: a mixed
        # collection where SOME players are implicated in a warning and
        # OTHERS are merely examined (read, compared, found to collide with
        # nothing) must audit every one of them for both categories, not
        # just the ones whose ids happen to appear in an emitted warning's
        # player_ids.
        p1, p2 = self._seed_pair()  # the one shared_registration_number pair
        unrelated1 = self.api.setup.add_player(
            "t1", None, Position.DEFENSE, first_name="Uma", last_name="Ortiz",
            registration_number="REG-UNRELATED-1", birthdate="2014-05-01")
        unrelated2 = self.api.setup.add_player(
            "t2", None, Position.FORWARD, first_name="Nia", last_name="Park",
            registration_number="REG-UNRELATED-2", birthdate="2014-06-02")

        report = self.api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", report)
        # Only the p1/p2 pair produces a warning -- unrelated1/2 collide
        # with nothing and appear in NO warning's player_ids.
        for w in report["warnings"]:
            self.assertNotIn(unrelated1.id, w["player_ids"])
            self.assertNotIn(unrelated2.id, w["player_ids"])

        all_ids = {f"player:{p.id}"
                  for p in (p1, p2, unrelated1, unrelated2)}
        rows = self.rows()
        reg_subjects = {r.subject_id for r in rows
                        if r.category == C.REGISTRATION_NUMBER}
        bd_subjects = {r.subject_id for r in rows
                      if r.category == C.BIRTHDATE}
        self.assertEqual(reg_subjects, all_ids,
                         "REGISTRATION_NUMBER audit must cover every "
                         "examined player, not just warning-implicated ones")
        self.assertEqual(bd_subjects, all_ids,
                         "BIRTHDATE audit must cover every examined player, "
                         "not just warning-implicated ones")
        for row in rows:
            self.assertEqual(row.outcome, ACCESS_ALLOWED)
        # No protected value (registration number or birthdate) ever rides
        # a log field.
        for row in rows:
            text = str(vars(row))
            self.assertNotIn(SENTINEL_REGISTRATION, text)
            self.assertNotIn("REG-UNRELATED", text)
            self.assertNotIn("2014-05-01", text)
            self.assertNotIn("2014-06-02", text)

    def test_birthdate_touching_warning_types_audit_the_implicated_players(self):
        # #424 round-N owner review finding 1: same_name_same_team_
        # undisambiguated and same_name_same_birthdate both hinge on a
        # birthdate comparison -- their player_ids must land in the
        # BIRTHDATE audit row's subjects, not just the collection-level "*".
        same_team_a = self.api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Sam", last_name="Lee")
        same_team_b = self.api.setup.add_player(
            "t1", None, Position.GOALIE, first_name="Sam", last_name="Lee")
        cross_team_a = self.api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Cross",
            last_name="Bday", birthdate=SENTINEL_BIRTHDATE)
        cross_team_b = self.api.setup.add_player(
            "t2", None, Position.FORWARD, first_name="Cross",
            last_name="Bday", birthdate=SENTINEL_BIRTHDATE)

        report = self.api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN, actor_user_id="user_admin")
        self.assertNotIn("error", report)
        types = {w["type"] for w in report["warnings"]}
        self.assertIn("same_name_same_team_undisambiguated", types)
        self.assertIn("same_name_same_birthdate", types)

        bd_rows = [r for r in self.rows() if r.category == C.BIRTHDATE]
        self.assertEqual(len(bd_rows), 4)
        self.assertEqual(
            {r.subject_id for r in bd_rows},
            {f"player:{same_team_a.id}", f"player:{same_team_b.id}",
             f"player:{cross_team_a.id}", f"player:{cross_team_b.id}"})

    def test_unauthorized_role_refuses_the_whole_call(self):
        self._seed_pair()
        result = self.api.player_duplicate_report(
            actor_role=Role.COACH, actor_user_id="user_coach")
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertNotIn("warnings", result)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].outcome, ACCESS_DENIED)
        self.assertEqual((rows[0].subject_type, rows[0].subject_id),
                         ("recipient", "*"))

    def test_no_principal_refuses_the_whole_call(self):
        self._seed_pair()
        result = self.api.player_duplicate_report()
        self.assertEqual(result["error"]["code"], "forbidden")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)

    def test_no_stored_row_contains_the_sentinel_value(self):
        self._seed_pair()
        self.api.player_duplicate_report(actor_role=Role.LEAGUE_ADMIN)
        self.api.player_duplicate_report(actor_role=Role.COACH)
        self.api.player_duplicate_report()
        for row in self.rows():
            self.assertNotIn(SENTINEL_REGISTRATION, str(vars(row)))
        # Positive control: the report ITSELF echoes the sentinel.
        report = self.api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN)
        dumped = json.dumps(report)
        self.assertIn(SENTINEL_REGISTRATION, dumped)


# =========================================================================== #
# #424 round-N+1 owner review finding A: same mixed-collection regression,   #
# proven on all three backends (the fix reads the SAME players_for_team/     #
# all_players collection SqlStore/InMemoryStore already serve identically). #
# =========================================================================== #
class PlayerDuplicateReportMixedCollectionTriBackendTest(unittest.TestCase):
    """Finding A's exact required regression, run for real against Memory,
    SQLite, AND PostgreSQL (not just the Memory-only fixture above): a
    collection containing one warning pair PLUS unrelated players with
    distinct identity values must audit every examined player -- not just
    the warning-implicated ones -- for both BIRTHDATE and
    REGISTRATION_NUMBER, and no protected value may ever appear in a log
    field."""

    def _backends(self):
        stores = [("memory", InMemoryStore()), ("sqlite", SqlStore(":memory:"))]
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            stores.append(("postgres", fresh_sql_store(url)))
        return stores

    def test_every_examined_player_audited_for_both_categories(self):
        for label, store in self._backends():
            with self.subTest(backend=label):
                try:
                    store.add_team(Team(id="t1", name="T1"))
                    store.add_team(Team(id="t2", name="T2"))
                    api = ApiService(store)
                    p1 = api.setup.add_player(
                        "t1", None, Position.FORWARD, first_name="Sam",
                        last_name="Lee",
                        registration_number="REG-SHARED-" + label)
                    p2 = api.setup.add_player(
                        "t2", None, Position.GOALIE, first_name="Alex",
                        last_name="Wu",
                        registration_number="REG-SHARED-" + label)
                    unrelated1 = api.setup.add_player(
                        "t1", None, Position.DEFENSE, first_name="Uma",
                        last_name="Ortiz",
                        registration_number="REG-U1-" + label,
                        birthdate="2014-05-01")
                    unrelated2 = api.setup.add_player(
                        "t2", None, Position.FORWARD, first_name="Nia",
                        last_name="Park",
                        registration_number="REG-U2-" + label,
                        birthdate="2014-06-02")

                    report = api.player_duplicate_report(
                        actor_role=Role.LEAGUE_ADMIN,
                        actor_user_id="user_admin")
                    self.assertNotIn("error", report, (label, report))
                    for w in report["warnings"]:
                        self.assertNotIn(unrelated1.id, w["player_ids"],
                                         label)
                        self.assertNotIn(unrelated2.id, w["player_ids"],
                                         label)

                    rows = store.list_data_access()
                    all_subjects = {f"player:{p.id}"
                                   for p in (p1, p2, unrelated1, unrelated2)}
                    reg_subjects = {r.subject_id for r in rows
                                   if r.category == C.REGISTRATION_NUMBER}
                    bd_subjects = {r.subject_id for r in rows
                                  if r.category == C.BIRTHDATE}
                    self.assertEqual(
                        reg_subjects, all_subjects,
                        f"[{label}] REGISTRATION_NUMBER must audit every "
                        "examined player, including non-warning ones")
                    self.assertEqual(
                        bd_subjects, all_subjects,
                        f"[{label}] BIRTHDATE must audit every examined "
                        "player, including non-warning ones")
                    for row in rows:
                        self.assertEqual(row.outcome, ACCESS_ALLOWED, label)
                        text = str(vars(row))
                        self.assertNotIn("REG-SHARED", text, label)
                        self.assertNotIn("REG-U1", text, label)
                        self.assertNotIn("REG-U2", text, label)
                        self.assertNotIn("2014-05-01", text, label)
                        self.assertNotIn("2014-06-02", text, label)
                finally:
                    if isinstance(store, SqlStore):
                        store.close()


def _fresh_eligibility_fixture():
    """A standalone, minimal fixture (own store/api/setup) for the
    role-matrix test below, which needs a FRESH player+store per role so
    each iteration's audit rows and eligibility state never leak into the
    next -- same minimal-fixture shape ``_IdentityAuditBase.setUp`` uses,
    factored out since the matrix test can't reuse ``self.store`` across
    ``subTest`` iterations without cross-contaminating row counts."""
    from datetime import datetime, timezone
    from hockey_scheduler.domain import Division, League, LeagueSeason, Season
    store = InMemoryStore()
    store.add_program(Program(id="pr", name="P"))
    store.add_season(Season(
        id="se", program_id="pr", name="Fall",
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    store.add_league(League(id="lg", program_id="pr", name="L"))
    store.add_league_season(
        LeagueSeason(id="ls", league_id="lg", season_id="se"))
    store.add_division(Division(id="d", league_season_id="ls", name="D1",
                                age_group="U10"))
    store.add_team(Team(id="t1", name="T1", program_id="pr"))
    store.add_team(Team(id="t2", name="T2", program_id="pr"))
    api = ApiService(store)
    return store, api, api.setup


# =========================================================================== #
# #424 audit-wiring: evaluate_player_eligibility — BIRTHDATE at SUMMARY       #
# fidelity (may_read_summary, not may_read_raw), whole-call refusal.         #
# =========================================================================== #
class EligibilitySummaryAuditTest(_IdentityAuditBase):
    def _seed(self):
        self.api.set_age_eligibility_rule(
            "ls", 12, 31, [{"code": "U10", "max_age": 30}])
        return self.api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Priv",
            last_name="Fields", birthdate=SENTINEL_BIRTHDATE)

    def test_coach_gets_summary_fidelity_and_one_allowed_row(self):
        p = self._seed()
        result = self.api.evaluate_player_eligibility(
            p.id, "d", actor_role=Role.COACH, actor_user_id="user_coach")
        self.assertNotIn("error", result)
        self.assertNotIn("birthdate", result)

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.category, C.BIRTHDATE)
        self.assertEqual(row.outcome, ACCESS_ALLOWED)
        self.assertEqual(row.actor_role, "coach")
        self.assertEqual(row.actor_user_id, "user_coach")
        self.assertEqual(row.subject_id, f"player:{p.id}")
        self.assertEqual(row.purpose, "evaluate_player_eligibility")

    def test_league_admin_raw_grant_also_covers_the_summary_gate(self):
        # may_read_summary(role, category) is >= SUMMARY: a RAW grant
        # (LEAGUE_ADMIN) satisfies it too, not only an exact SUMMARY match.
        p = self._seed()
        result = self.api.evaluate_player_eligibility(
            p.id, "d", include_details=True, actor_role=Role.LEAGUE_ADMIN,
            actor_user_id="user_admin")
        self.assertNotIn("error", result)
        self.assertIsNotNone(result.get("age_at_cutoff"))

    def test_coach_requesting_details_gets_summary_masked_not_refused(self):
        # #424 round-N owner review finding 3: COACH holds SUMMARY (not
        # RAW) on BIRTHDATE, so include_details=True must NOT hand COACH
        # the operator-only age_at_cutoff/cutoff_date fields -- but the
        # call itself still succeeds with the SUMMARY-safe subset (mask,
        # not refuse), since that subset remains meaningful on its own.
        p = self._seed()
        result = self.api.evaluate_player_eligibility(
            p.id, "d", include_details=True, actor_role=Role.COACH,
            actor_user_id="user_coach")
        self.assertNotIn("error", result, result)
        self.assertNotIn("age_at_cutoff", result)
        self.assertNotIn("cutoff_date", result)
        # The SUMMARY-safe fields are still present and correct.
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(result["tier_code"], "U10")

        rows = self.rows()
        self.assertEqual(len(rows), 2)
        summary_rows = [r for r in rows
                        if r.purpose == "evaluate_player_eligibility"]
        detail_rows = [r for r in rows
                       if r.purpose == "evaluate_player_eligibility_details"]
        self.assertEqual(len(summary_rows), 1)
        self.assertEqual(summary_rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(len(detail_rows), 1)
        self.assertEqual(detail_rows[0].outcome, ACCESS_DENIED)
        self.assertEqual(detail_rows[0].actor_role, "coach")
        self.assertEqual(detail_rows[0].subject_id, f"player:{p.id}")

    def test_league_admin_requesting_details_gets_an_allowed_detail_row(self):
        p = self._seed()
        self.api.evaluate_player_eligibility(
            p.id, "d", include_details=True, actor_role=Role.LEAGUE_ADMIN,
            actor_user_id="user_admin")
        rows = self.rows()
        detail_rows = [r for r in rows
                       if r.purpose == "evaluate_player_eligibility_details"]
        self.assertEqual(len(detail_rows), 1)
        self.assertEqual(detail_rows[0].outcome, ACCESS_ALLOWED)
        self.assertEqual(detail_rows[0].category, C.BIRTHDATE)

    def test_include_details_false_never_writes_a_detail_row(self):
        p = self._seed()
        for role in (Role.COACH, Role.LEAGUE_ADMIN):
            self.api.evaluate_player_eligibility(
                p.id, "d", actor_role=role, actor_user_id="u")
        self.assertEqual(
            [r for r in self.rows()
             if r.purpose == "evaluate_player_eligibility_details"], [])

    def test_role_matrix_summary_vs_raw_tier_for_every_role(self):
        # #424 round-N owner review finding 3: enumerate every role's
        # actual grant/deny/tier at BOTH the SUMMARY (whole-call) gate and
        # the RAW (detail-field) gate, mirroring test_privacy_policy.py's
        # PolicyMatrixTest convention -- no sampling.
        EXPECTED = {
            Role.LEAGUE_ADMIN: ("summary_ok", "details_ok"),
            Role.ARENA_MANAGER: ("refused", None),
            Role.COACH: ("summary_ok", "details_masked"),
            Role.PLAYER: ("refused", None),
            Role.GUARDIAN: ("refused", None),
            Role.OFFICIAL: ("refused", None),
            Role.VIEWER: ("refused", None),
        }
        self.assertEqual(set(EXPECTED), set(Role))
        for role, (whole_call, detail_tier) in EXPECTED.items():
            with self.subTest(role=role.value):
                store, api, setup = _fresh_eligibility_fixture()
                setup.set_age_eligibility_rule(
                    "ls", 12, 31, [{"code": "U10", "max_age": 30}])
                p = setup.add_player(
                    "t1", None, Position.FORWARD, first_name="Priv",
                    last_name="Fields", birthdate=SENTINEL_BIRTHDATE)
                result = api.evaluate_player_eligibility(
                    p.id, "d", include_details=True, actor_role=role,
                    actor_user_id=f"user_{role.value}")
                if whole_call == "refused":
                    self.assertEqual(result["error"]["code"], "forbidden",
                                     role.value)
                else:
                    self.assertNotIn("error", result, (role.value, result))
                    if detail_tier == "details_ok":
                        self.assertIsNotNone(result.get("age_at_cutoff"),
                                             role.value)
                        self.assertIsNotNone(result.get("cutoff_date"),
                                             role.value)
                    else:
                        self.assertIsNone(result.get("age_at_cutoff"),
                                          role.value)
                        self.assertIsNone(result.get("cutoff_date"),
                                          role.value)
                        self.assertEqual(result["status"], "eligible",
                                         role.value)

    def test_unauthorized_role_refuses_the_whole_call_default_and_details(self):
        p = self._seed()
        for kwargs in ({}, {"include_details": True}):
            result = self.api.evaluate_player_eligibility(
                p.id, "d", actor_role=Role.VIEWER,
                actor_user_id="user_viewer", **kwargs)
            self.assertEqual(result["error"]["code"], "forbidden")
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.outcome == ACCESS_DENIED for r in rows))

    def test_no_principal_refuses_the_whole_call(self):
        p = self._seed()
        result = self.api.evaluate_player_eligibility(p.id, "d")
        self.assertEqual(result["error"]["code"], "forbidden")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].actor_role, vp.NO_PRINCIPAL)

    def test_no_stored_row_contains_the_sentinel_birthdate(self):
        p = self._seed()
        self.api.evaluate_player_eligibility(p.id, "d",
                                             actor_role=Role.COACH)
        self.api.evaluate_player_eligibility(p.id, "d",
                                             actor_role=Role.VIEWER)
        self.api.evaluate_player_eligibility(p.id, "d")
        for row in self.rows():
            self.assertNotIn(SENTINEL_BIRTHDATE, str(vars(row)))
        # Positive control: the sentinel lives on the stored Player.
        self.assertEqual(self.store.get_player(p.id).birthdate,
                         SENTINEL_BIRTHDATE)


# =========================================================================== #
# #424 audit-wiring: durability across a real database (SQLite/PostgreSQL),  #
# mirroring SqliteFacadeDurabilityTest/PostgresFacadeDurabilityTest above.    #
# =========================================================================== #
class SqliteIdentityAuditDurabilityTest(unittest.TestCase):
    def _store(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(os.remove, self._tmp)
        self._reopen = lambda: SqlStore(self._tmp)
        return SqlStore(self._tmp)

    def test_all_three_call_sites_survive_reopen(self):
        store = self._store()
        api = ApiService(store)
        store.add_program(Program(id="pr", name="P"))
        store.add_team(Team(id="t1", name="T1", program_id="pr"))
        p = api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Priv",
            last_name="Fields", birthdate=SENTINEL_BIRTHDATE,
            registration_number=SENTINEL_REGISTRATION)

        self.assertNotIn("error", api.list_players(
            team_id="t1", include_identity=True, role=Role.LEAGUE_ADMIN,
            user_id="user_admin"))
        api.list_players(team_id="t1", include_identity=True,
                         role=Role.COACH)
        self.assertNotIn("error", api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN))
        refused = api.player_duplicate_report(actor_role=Role.COACH)
        self.assertEqual(refused["error"]["code"], "forbidden")
        store.close()

        reopened = self._reopen()
        self.addCleanup(reopened.close)
        rows = reopened.list_data_access()
        # 2 (identity ALLOW) + 2 (identity DENY) + 2 (dup ALLOW: REGISTRATION_
        # NUMBER + BIRTHDATE, #424 round-N owner review finding 1) +
        # 1 (dup DENY: whole-call refusal writes one row, on the first
        # denied category checked)
        self.assertEqual(len(rows), 7)
        outcomes = {r.outcome for r in rows}
        self.assertEqual(outcomes, {ACCESS_ALLOWED, ACCESS_DENIED})
        for row in rows:
            text = str(vars(row))
            self.assertNotIn(SENTINEL_BIRTHDATE, text)
            self.assertNotIn(SENTINEL_REGISTRATION, text)
        self.assertEqual(reopened.get_player(p.id).birthdate,
                         SENTINEL_BIRTHDATE)


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "PostgreSQL suite only (TEST_DATABASE_URL not set)")
class PostgresIdentityAuditDurabilityTest(unittest.TestCase):
    def test_all_three_call_sites_survive_reopen(self):
        url = os.environ["TEST_DATABASE_URL"]
        store = fresh_sql_store(url)
        api = ApiService(store)
        store.add_program(Program(id="pr", name="P"))
        store.add_team(Team(id="t1", name="T1", program_id="pr"))
        p = api.setup.add_player(
            "t1", None, Position.FORWARD, first_name="Priv",
            last_name="Fields", birthdate=SENTINEL_BIRTHDATE,
            registration_number=SENTINEL_REGISTRATION)

        self.assertNotIn("error", api.list_players(
            team_id="t1", include_identity=True, role=Role.LEAGUE_ADMIN,
            user_id="user_admin"))
        api.list_players(team_id="t1", include_identity=True,
                         role=Role.COACH)
        self.assertNotIn("error", api.player_duplicate_report(
            actor_role=Role.LEAGUE_ADMIN))
        refused = api.player_duplicate_report(actor_role=Role.COACH)
        self.assertEqual(refused["error"]["code"], "forbidden")
        store.close()

        reopened = SqlStore(url)
        self.addCleanup(reopened.close)
        rows = reopened.list_data_access()
        # 2 (identity ALLOW) + 2 (identity DENY) + 2 (dup ALLOW: REGISTRATION_
        # NUMBER + BIRTHDATE, #424 round-N owner review finding 1) +
        # 1 (dup DENY: whole-call refusal writes one row, on the first
        # denied category checked)
        self.assertEqual(len(rows), 7)
        self.assertEqual({r.outcome for r in rows},
                         {ACCESS_ALLOWED, ACCESS_DENIED})
        for row in rows:
            text = str(vars(row))
            self.assertNotIn(SENTINEL_BIRTHDATE, text)
            self.assertNotIn(SENTINEL_REGISTRATION, text)
        self.assertEqual(reopened.get_player(p.id).birthdate,
                         SENTINEL_BIRTHDATE)


if __name__ == "__main__":
    unittest.main()
