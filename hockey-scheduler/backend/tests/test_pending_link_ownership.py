"""Lifecycle matrix for the creator-owned ``pending_link_*`` contract (#367).

``get_setup_overview_v2`` omits every record with no validated link to any
Program, EXCEPT the ones this caller created — those come back additively in
``pending_link_<entity>`` so a create-then-link flow can still reach its own
fresh Club/Organization/Official/Venue/Rink/IceSlot. The #367 owner ruling
permits that narrow contract only under conditions, and this module is the
lifecycle half of proving them. ``test_league_filtered_overview_v2.py`` covers
the SCOPING half (which records are linked to which Program); everything here
is about WHO owns an unlinked record and for how long:

* **foreign creator** — a second real operator account never receives the
  first's pending rows, in either direction;
* **zero scope** — an identity authorized for zero Programs (while other
  Programs exist) receives nothing, which is the exact caller the reversed
  ``unassigned_*`` mechanism answered with the installation's whole unlinked
  inventory;
* **``user_id=None``** — an identity-less caller owns nothing, rather than
  matching every ``actor_id=None`` row an internal/seed path ever wrote;
* **deleted/deactivated creator** — decided behavior, asserted below;
* **post-link** — the moment a validated Program link exists the row LEAVES
  ``pending_link_*``; it reappears in the normal scoped list only for the
  Program it is now linked to;
* **no ownership by probing** — creating a child under a guessed parent id,
  and any non-create audit action, both fail to disclose anything.

**Deactivated/deleted creator: the decided behavior.** A deactivated
account's pending rows become visible to NOBODY. They are keyed to an
``actor_id`` that is that account's own id, which no other session can ever
present (``_resolve_role`` reads ``user_id`` from the server-side session
record, and deactivating an account revokes its sessions and blocks re-login),
so the rows fail CLOSED: not inherited by the admin who deactivated the
account, not released to the installation, not visible to a later account that
reuses the username. Reactivation restores exactly the creator's own view and
nobody else's. There is deliberately NO "creator account must still exist"
check inside the read: the facade's ``user_id`` IS the caller's proven
identity, established at the HTTP boundary, and re-deriving authorization from
account state would make the read depend on a second, weaker signal while
adding nothing — an attacker who could present the deactivated account's
``user_id`` to the facade has already bypassed authentication entirely. Account
ids are never recycled (``next_id`` is a monotonic counter on every store, and
a new account for a reused username gets a fresh id), so no later account can
inherit a deactivated one's rows by id collision either. All four halves are
asserted below, facade-level and over authenticated HTTP.

Memory/SQLite/(optional)PostgreSQL for the facade contracts, plus a real
authenticated-HTTP class for the ones that only exist across the route
boundary: that the ``actor_id`` the write route records and the ``user_id``
the read route resolves are the SAME session account id, that a deactivated
account genuinely cannot come back, and that the demo seed is attributed to a
REAL account rather than a synthetic label.
"""

import http.client
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND, fresh_sql_store  # noqa: F401  (sets up sys.path)

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import Role, SetupAuditLog
from hockey_scheduler.full_demo import DEMO_ADMIN_ACCOUNT_ID
from hockey_scheduler.store import InMemoryStore, SqlStore

_PENDING_KEYS = (
    "pending_link_clubs", "pending_link_organizations",
    "pending_link_officials", "pending_link_venues", "pending_link_rinks",
    "pending_link_ice_slots",
)
# Each pending list and the scoped list a row moves INTO once it is linked.
_PENDING_TO_SCOPED = {
    "pending_link_clubs": "clubs",
    "pending_link_organizations": "organizations",
    "pending_link_officials": "officials",
    "pending_link_venues": "venues",
    "pending_link_rinks": "rinks",
    "pending_link_ice_slots": "ice_slots",
}


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield "postgres", fresh_sql_store(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


class PendingLinkOwnershipTest(unittest.TestCase):
    """Facade-level, across the full store matrix."""

    @staticmethod
    def _ids(ov, key):
        return {row["id"] for row in ov.get(key, [])}

    def _program_env(self, api, label, actor_id):
        """A minimal but COMPLETE active tuple: Organization → Program →
        Season → League → Division → Club → Team → registration. Enough for
        ``resolve_with_league`` to land on a real Program+Season+League, which
        every scoped assertion below depends on."""
        org = api.create_organization(f"Op Org {label}", actor_id=actor_id)
        self.assertNotIn("error", org, org)
        program = api.create_program(f"Program {label}", country="US",
                                     operator_organization_id=org["id"],
                                     actor_id=actor_id)
        self.assertNotIn("error", program, program)
        season = api.create_season(program["id"], f"Season {label}",
                                   actor_id=actor_id)
        self.assertNotIn("error", season, season)
        league = api.create_league(season["id"], f"League {label}",
                                   actor_id=actor_id)
        self.assertNotIn("error", league, league)
        division = api.create_division(season["id"], f"Division {label}",
                                       league_id=league["id"],
                                       actor_id=actor_id)
        self.assertNotIn("error", division, division)
        club = api.create_club(f"Anchor Club {label}", actor_id=actor_id)
        self.assertNotIn("error", club, club)
        team = api.create_team(club_id=club["id"], division_id=division["id"],
                               name=f"Anchor Team {label}",
                               program_id=program["id"],
                               league_id=league["id"], actor_id=actor_id)
        self.assertNotIn("error", team, team)
        reg = api.register_team_for_season(season["id"], team["id"],
                                           division["id"],
                                           league_id=league["id"],
                                           actor_id=actor_id)
        self.assertNotIn("error", reg, reg)
        return {"org": org, "program": program, "season": season,
                "league": league, "division": division, "club": club,
                "team": team}

    def _unlinked_set(self, api, actor_id, tag):
        """One never-linked record of every pending-link kind, all created by
        ``actor_id``. Returns ``{pending_key: id}``."""
        club = api.create_club(f"{tag} Club", actor_id=actor_id)
        org = api.create_organization(f"{tag} Org", actor_id=actor_id)
        # Owned by `org` on purpose: the Organization is unlinked only while
        # every Venue it owns is unlinked too, so this keeps the Venue and
        # Organization rows on the SAME lifecycle and lets one grant below
        # move both at once.
        venue = api.create_venue(f"{tag} Venue", organization_id=org["id"],
                                 actor_id=actor_id)
        rink = api.create_rink(venue["id"], f"{tag} Rink", actor_id=actor_id)
        slot = api.create_ice_slot(rink["id"], "2027-03-01T18:00:00+00:00",
                                   "2027-03-01T19:30:00+00:00",
                                   actor_id=actor_id)
        official = api.create_official(f"{tag} Official",
                                       home_club_id=club["id"],
                                       actor_id=actor_id)
        for r in (club, org, venue, rink, slot, official):
            self.assertNotIn("error", r, r)
        return {"pending_link_clubs": club["id"],
                "pending_link_organizations": org["id"],
                "pending_link_venues": venue["id"],
                "pending_link_rinks": rink["id"],
                "pending_link_ice_slots": slot["id"],
                "pending_link_officials": official["id"]}

    def _activate(self, api, user_id, role, env):
        ctx = api.set_active_context(user_id, role, {}, env["program"]["id"],
                                     env["season"]["id"], None)
        self.assertNotIn("error", ctx, ctx)

    def _overview(self, api, user_id, role=Role.LEAGUE_ADMIN):
        ov = api.get_setup_overview_v2(user_id, role, {})
        self.assertNotIn("error", ov, ov)
        return ov

    # -- foreign creator ----------------------------------------------------
    def test_a_foreign_creator_never_receives_another_operators_pending_rows(self):
        """Two real operator accounts in ONE installation, each with its own
        Program and its own set of never-linked records. Neither may see the
        other's, in EITHER direction — including the direction where the
        reader is the more broadly authorized of the two, since authorization
        breadth is not what this contract keys on."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                alice, bob = "user_alice", "user_bob"
                env_a = self._program_env(api, "A", alice)
                env_b = self._program_env(api, "B", bob)
                mine = self._unlinked_set(api, alice, "Alice")
                theirs = self._unlinked_set(api, bob, "Bob")

                self._activate(api, alice, Role.LEAGUE_ADMIN, env_a)
                self._activate(api, bob, Role.LEAGUE_ADMIN, env_b)

                for reader, own, foreign, other_label in (
                        (alice, mine, theirs, "Bob"),
                        (bob, theirs, mine, "Alice")):
                    ov = self._overview(api, reader)
                    for key in _PENDING_KEYS:
                        self.assertIn(
                            own[key], self._ids(ov, key),
                            f"[{label}] {reader} lost sight of the record it "
                            f"created in {key} — create-then-link cannot work")
                        self.assertNotIn(
                            foreign[key], self._ids(ov, key),
                            f"[{label}] {reader} enumerated {other_label}'s "
                            f"never-linked record in {key} — a second "
                            "operator must never receive rows it did not "
                            "create")
                        # Nor through the scoped list: the row is unlinked, so
                        # it belongs in NEITHER list for a foreign reader.
                        self.assertNotIn(
                            foreign[key],
                            self._ids(ov, _PENDING_TO_SCOPED[key]),
                            f"[{label}] {reader} received {other_label}'s "
                            f"never-linked record through "
                            f"{_PENDING_TO_SCOPED[key]}")
                _close(store)

    # -- zero scope / identity-less ----------------------------------------
    def test_a_zero_scope_identity_and_a_none_user_id_both_receive_nothing(self):
        """The two fail-closed cases, together because they share a victim
        set. A caller authorized for ZERO Programs — while other Programs DO
        exist, so this is denial and not bootstrap — is exactly the identity
        the ruling names, and ``user_id=None`` must own nothing rather than
        matching every ``actor_id=None`` row a seed/internal path wrote."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                creator = "user_creator"
                env = self._program_env(api, "A", creator)
                created = self._unlinked_set(api, creator, "Owned")
                # A row with NO actor at all — the shape an internal/seed call
                # path leaves behind. It must not attach to the None caller.
                orphan = api.create_club("Actorless Club")
                self.assertNotIn("error", orphan, orphan)
                self._activate(api, creator, Role.LEAGUE_ADMIN, env)

                zero = self._overview(api, "user_zero_scope", Role.COACH)
                for key in _PENDING_KEYS:
                    self.assertEqual(
                        zero[key], [],
                        f"[{label}] an identity authorized for ZERO Programs "
                        f"received {key} — this is the installation-wide "
                        "disclosure the #367 ruling reverses")

                anon = self._overview(api, None, Role.LEAGUE_ADMIN)
                for key in _PENDING_KEYS:
                    self.assertEqual(
                        anon[key], [],
                        f"[{label}] user_id=None was handed {key}; an "
                        "identity-less caller must own nothing")
                self.assertNotIn(
                    orphan["id"], self._ids(anon, "pending_link_clubs"),
                    f"[{label}] user_id=None matched an actor_id=None row")

                # Positive control: these lists are not empty by construction —
                # the creator itself does receive every one of them.
                own = self._overview(api, creator)
                for key in _PENDING_KEYS:
                    self.assertIn(created[key], self._ids(own, key),
                                  (label, key))
                _close(store)

    # -- deactivated / deleted creator --------------------------------------
    def test_a_deactivated_creators_pending_rows_are_released_to_nobody(self):
        """The decided behavior (see module docstring): deactivating the
        creator must not hand its unlinked records to anyone else — not to the
        admin who deactivated it, not to a later account that takes over the
        same username, and not back into any installation-wide pool. The rows
        simply stop being reachable, and reactivation restores exactly the
        creator's own view."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                admin = api.create_user_account("boss", "correct-horse-1",
                                                "league_admin")
                self.assertNotIn("error", admin, admin)
                creator = api.create_user_account("worker", "correct-horse-2",
                                                  "arena_manager")
                self.assertNotIn("error", creator, creator)
                env = self._program_env(api, "A", admin["id"])
                created = self._unlinked_set(api, creator["id"], "Worker")
                self._activate(api, admin["id"], Role.LEAGUE_ADMIN, env)
                self._activate(api, creator["id"], Role.ARENA_MANAGER, env)

                before = self._overview(api, creator["id"], Role.ARENA_MANAGER)
                for key in _PENDING_KEYS:
                    self.assertIn(created[key], self._ids(before, key),
                                  (label, key, "creator's own rows pre-check"))

                res = api.set_user_account_active(creator["id"], False,
                                                  actor_id=admin["id"])
                self.assertNotIn("error", res, res)
                self.assertFalse(res["active"], res)

                # (a) The admin who deactivated the account does NOT inherit
                #     them.
                ov_admin = self._overview(api, admin["id"])
                for key in _PENDING_KEYS:
                    self.assertNotIn(
                        created[key], self._ids(ov_admin, key),
                        f"[{label}] deactivating an account released its "
                        f"never-linked {key} row to the admin who "
                        "deactivated it")
                    self.assertNotIn(
                        created[key],
                        self._ids(ov_admin, _PENDING_TO_SCOPED[key]),
                        f"[{label}] a deactivated account's never-linked row "
                        f"surfaced through {_PENDING_TO_SCOPED[key]}")

                # (b) A NEW account is a new identity, even reusing the freed
                #     username — ids are never recycled, so it inherits
                #     nothing.
                res = api.set_user_account_active(creator["id"], False,
                                                  actor_id=admin["id"])
                successor = api.create_user_account(
                    "worker2", "correct-horse-3", "arena_manager")
                self.assertNotIn("error", successor, successor)
                self.assertNotEqual(successor["id"], creator["id"], label)
                self._activate(api, successor["id"], Role.ARENA_MANAGER, env)
                ov_successor = self._overview(api, successor["id"],
                                              Role.ARENA_MANAGER)
                for key in _PENDING_KEYS:
                    self.assertNotIn(
                        created[key], self._ids(ov_successor, key),
                        f"[{label}] a successor account inherited a "
                        f"deactivated creator's {key} row")

                # (c) Reactivation restores the creator's OWN view, and only
                #     it — the rows were never reassigned in the meantime.
                res = api.set_user_account_active(creator["id"], True,
                                                  actor_id=admin["id"])
                self.assertNotIn("error", res, res)
                after = self._overview(api, creator["id"], Role.ARENA_MANAGER)
                for key in _PENDING_KEYS:
                    self.assertIn(
                        created[key], self._ids(after, key),
                        f"[{label}] reactivating the creator did not restore "
                        f"its own {key} row")
                _close(store)

    # -- post-link ----------------------------------------------------------
    def test_a_record_leaves_pending_link_the_moment_a_program_link_exists(self):
        """The ruling's "removed/ceased once a validated Program link exists".

        Every kind starts unlinked and owned by the caller; then ONE real link
        per kind is made and the row must (a) disappear from
        ``pending_link_*`` and (b) reappear in the normal scoped list for the
        Program it is now linked to. Both halves matter: dropping only (a)
        would make a linked record invisible, and dropping only (b) would keep
        offering it on the creator-ownership ticket forever."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                creator = "user_creator"
                env = self._program_env(api, "A", creator)
                created = self._unlinked_set(api, creator, "Linkable")
                self._activate(api, creator, Role.LEAGUE_ADMIN, env)

                before = self._overview(api, creator)
                for key in _PENDING_KEYS:
                    self.assertIn(created[key], self._ids(before, key),
                                  (label, key, "unlinked pre-check"))
                    self.assertNotIn(
                        created[key],
                        self._ids(before, _PENDING_TO_SCOPED[key]),
                        (label, key, "must not be in the scoped list yet"))

                # The links, one per kind, all into the ACTIVE tuple:
                #  * Club — a Team of the active Program+League in it. That
                #    same Team also links the Official homed at that Club.
                #  * Venue — an active SeasonVenueAccess grant to the active
                #    Season; the Rink and IceSlot cascade from their Venue,
                #    and the owning Organization links through the Venue.
                team = api.create_team(club_id=created["pending_link_clubs"],
                                       division_id=env["division"]["id"],
                                       name="Linking Team",
                                       program_id=env["program"]["id"],
                                       league_id=env["league"]["id"],
                                       actor_id=creator)
                self.assertNotIn("error", team, team)
                grant = api.grant_season_venue_access(
                    env["season"]["id"], created["pending_link_venues"],
                    actor_id=creator)
                self.assertNotIn("error", grant, grant)

                after = self._overview(api, creator)
                for key in _PENDING_KEYS:
                    self.assertNotIn(
                        created[key], self._ids(after, key),
                        f"[{label}] a record with a validated Program link is "
                        f"still offered through {key} — the creator-owned "
                        "list must CEASE at the first real link")
                    self.assertIn(
                        created[key],
                        self._ids(after, _PENDING_TO_SCOPED[key]),
                        f"[{label}] a newly-linked record vanished: it left "
                        f"{key} without arriving in "
                        f"{_PENDING_TO_SCOPED[key]}")

                # ...and it is the ACTIVE Program's data now, so a second
                # operator working in a DIFFERENT Program still does not get
                # it — through either list.
                other = "user_other"
                env_b = self._program_env(api, "B", other)
                self._activate(api, other, Role.LEAGUE_ADMIN, env_b)
                ov_other = self._overview(api, other)
                for key in _PENDING_KEYS:
                    self.assertNotIn(created[key], self._ids(ov_other, key),
                                     (label, key, "foreign reader, pending"))
                    self.assertNotIn(
                        created[key],
                        self._ids(ov_other, _PENDING_TO_SCOPED[key]),
                        f"[{label}] a record linked to Program A appeared in "
                        f"Program B's {_PENDING_TO_SCOPED[key]}")
                _close(store)

    # -- no ownership, and no disclosure, by probing an id ------------------
    def test_creating_a_child_under_a_guessed_parent_id_discloses_no_name(self):
        """Ownership of a pending row authorizes THE ROW, never the foreign
        record it points at.

        Entity ids are sequential (``next_id`` counts per prefix), and every
        create route takes its parent id straight from the request body
        without checking that the caller may see that parent. So an operator
        holding only MANAGE_ARENA — which covers the venue/rink/ice-slot/
        organization creates AND this read, at zero authorized Programs — can
        POST a Rink at ``venue_N``, an IceSlot at ``rink_N``, a Venue at
        ``org_N`` and an Official homed at ``club_N``, then read back its OWN
        pending rows. Those rows are legitimately its own; the resolved
        ``venue_name`` / ``rink_name`` / ``organization_name`` /
        ``home_club_name`` on them are not, and each one used to hand over a
        different operator's never-linked record BY NAME — the precise
        disclosure the #367 ruling reversed ``unassigned_*`` to stop."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                victim, attacker = "user_victim", "user_attacker"
                # Another Program must exist, or the attacker's read would be
                # a zero-Program BOOTSTRAP (legitimately unfiltered) instead
                # of the denial this asserts.
                env = self._program_env(api, "A", victim)
                secret = self._unlinked_set(api, victim, "SECRET")

                probes = (
                    ("pending_link_rinks", "venue_name",
                     lambda: api.create_rink(secret["pending_link_venues"],
                                             "probe-rink", actor_id=attacker)),
                    ("pending_link_ice_slots", "rink_name",
                     lambda: api.create_ice_slot(
                         secret["pending_link_rinks"],
                         "2027-04-01T10:00:00+00:00",
                         "2027-04-01T11:00:00+00:00", actor_id=attacker)),
                    ("pending_link_venues", "organization_name",
                     lambda: api.create_venue(
                         "probe-venue",
                         organization_id=secret["pending_link_organizations"],
                         actor_id=attacker)),
                    ("pending_link_officials", "home_club_name",
                     lambda: api.create_official(
                         "probe-official",
                         home_club_id=secret["pending_link_clubs"],
                         actor_id=attacker)),
                )
                for key, name_field, probe in probes:
                    made = probe()
                    self.assertNotIn("error", made, (label, key, made))
                ov = self._overview(api, attacker, Role.ARENA_MANAGER)

                for key, name_field, _probe in probes:
                    rows = ov[key]
                    self.assertTrue(
                        rows, (label, key, "probe row missing — the probe "
                                           "itself did not run"))
                    for row in rows:
                        self.assertIsNone(
                            row[name_field],
                            f"[{label}] probing a guessed parent id disclosed "
                            f"another operator's never-linked record by name "
                            f"through {key}.{name_field}: {row[name_field]!r}")
                        self.assertNotIn(
                            "SECRET", json.dumps(row),
                            f"[{label}] a victim's record name reached the "
                            f"attacker's {key} row: {row}")
                # The victim's own rows never entered the attacker's payload
                # in their own right either.
                for key in _PENDING_KEYS:
                    self.assertNotIn(secret[key], self._ids(ov, key),
                                     (label, key))
                # Positive control: the SAME name fields DO resolve on rows
                # whose parent this caller can see, so the assertions above
                # are not passing because the field is always None.
                own = self._unlinked_set(api, attacker, "Own")
                ov = self._overview(api, attacker, Role.ARENA_MANAGER)
                resolved = {
                    "pending_link_venues": "organization_name",
                    "pending_link_rinks": "venue_name",
                    "pending_link_ice_slots": "rink_name",
                    "pending_link_officials": "home_club_name"}
                for key, name_field in resolved.items():
                    row = next(r for r in ov[key] if r["id"] == own[key])
                    self.assertTrue(
                        row[name_field],
                        f"[{label}] the caller's OWN create-then-link chain "
                        f"lost its parent label on {key}.{name_field}")
                _close(store)

    def test_no_audit_action_other_than_the_create_entry_confers_ownership(self):
        """Replaying/poking an id must never become permission to enumerate
        it. Ownership is read from ``<entity>_created`` ONLY, so every OTHER
        audit action an attacker could get its own ``actor_id`` onto — a
        rename, a reassign, a delete, even a forged ``*_created`` under a
        MISMATCHED entity_type — confers nothing."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                victim, attacker = "user_victim", "user_attacker"
                env = self._program_env(api, "A", victim)
                secret = self._unlinked_set(api, victim, "Secret")
                self._activate(api, attacker, Role.LEAGUE_ADMIN, env)

                # Audit rows written straight to the store: what is under test
                # is how this READ interprets them, not which write path could
                # produce each one.
                forged = [
                    ("club_updated", "club", secret["pending_link_clubs"]),
                    ("club_deleted", "club", secret["pending_link_clubs"]),
                    ("venue_reassigned", "venue",
                     secret["pending_link_venues"]),
                    ("rink_updated", "rink", secret["pending_link_rinks"]),
                    ("ice_slot_updated", "ice_slot",
                     secret["pending_link_ice_slots"]),
                    ("official_updated", "official",
                     secret["pending_link_officials"]),
                    ("organization_updated", "organization",
                     secret["pending_link_organizations"]),
                    # An `<x>_created` action filed under a DIFFERENT
                    # entity_type: the match must require both to agree.
                    ("club_created", "venue", secret["pending_link_venues"]),
                    ("venue_created", "club", secret["pending_link_clubs"]),
                ]
                with store.transaction():
                    for action, entity_type, entity_id in forged:
                        store.add_setup_audit(SetupAuditLog(
                            id=store.next_id("audit"), action=action,
                            entity_type=entity_type, entity_id=entity_id,
                            at=datetime(2027, 5, 1, tzinfo=timezone.utc),
                            actor_id=attacker, detail={}))

                ov = self._overview(api, attacker)
                for key in _PENDING_KEYS:
                    self.assertEqual(
                        ov[key], [],
                        f"[{label}] an audit action OTHER than the create "
                        f"entry conferred ownership of {key} — a write-side "
                        "IDOR became a read-side one")
                # Positive control: a real create entry by this same actor DOES
                # show up, so the emptiness above is the filter, not the setup.
                own = api.create_club("Attacker's Own", actor_id=attacker)
                self.assertNotIn("error", own, own)
                ov = self._overview(api, attacker)
                self.assertEqual(self._ids(ov, "pending_link_clubs"),
                                 {own["id"]}, (label, ov))
                _close(store)


class PendingLinkOwnershipHttpTest(unittest.TestCase):
    """The halves that exist only across the route boundary.

    A facade test supplies both the write's ``actor_id`` and the read's
    ``user_id`` by hand and would agree with itself even if production wired
    two different values. Only here is the linkage real: both come from the
    server-resolved session, and only here can "a deactivated account cannot
    come back" and "the demo seed names a REAL account" be asserted at all.
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
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except (urllib.error.URLError, http.client.HTTPException) as e:
            # A transport-level failure is a RESULT, not a harness crash:
            # report it as a synthetic status so the assertion that was being
            # made produces its own message instead of an opaque traceback
            # (a route that drops the connection has still failed the check).
            return 0, {"transport_error": repr(e)}

    def _login(self, username, password="demo"):
        c = self._client()
        status, resp = self._req(c, "POST", "/api/auth/login",
                                 {"username": username, "password": password})
        self.assertEqual(status, 200, resp)
        return c

    def _post(self, c, entity, body):
        status, resp = self._req(c, "POST", f"/api/v2/setup/{entity}", body)
        self.assertEqual(status, 200, (entity, resp))
        self.assertNotIn("error", resp, (entity, resp))
        return resp

    def _overview(self, c):
        status, ov = self._req(c, "GET", "/api/v2/setup/overview")
        self.assertEqual(status, 200, ov)
        self.assertNotIn("error", ov, ov)
        return ov

    @staticmethod
    def _ids(ov, key):
        return {row["id"] for row in ov.get(key, [])}

    def _make_operator(self, admin, username, password, role):
        status, acct = self._req(admin, "POST", "/api/accounts",
                                 {"username": username, "password": password,
                                  "role": role, "scope": {}})
        self.assertEqual(status, 200, acct)
        self.assertNotIn("error", acct, acct)
        return acct

    def test_http_foreign_creator_and_post_link_over_real_sessions(self):
        """Two real sessions: each sees only what IT created, and a row that
        gains a real Program link leaves the pending list and lands in the
        scoped one — the whole lifecycle driven only by session identity."""
        admin = self._login("admin")
        second = self._make_operator(admin, "pl_second", "correct-horse-4",
                                     "league_admin")
        other = self._login("pl_second", "correct-horse-4")

        club = self._post(admin, "club", {"name": "HTTP-PL-Club"})
        venue = self._post(admin, "venue", {"name": "HTTP-PL-Venue"})
        rink = self._post(admin, "rink", {"venue_id": venue["id"],
                                          "name": "HTTP-PL-Rink"})
        official = self._post(admin, "official",
                              {"name": "HTTP-PL-Official",
                               "home_club_id": club["id"]})

        own = self._overview(admin)
        for key, rid in (("pending_link_clubs", club["id"]),
                         ("pending_link_venues", venue["id"]),
                         ("pending_link_rinks", rink["id"]),
                         ("pending_link_officials", official["id"])):
            self.assertIn(rid, self._ids(own, key), (key, own[key]))

        ov_other = self._overview(other)
        for key, rid in (("pending_link_clubs", club["id"]),
                         ("pending_link_venues", venue["id"]),
                         ("pending_link_rinks", rink["id"]),
                         ("pending_link_officials", official["id"])):
            self.assertNotIn(
                rid, self._ids(ov_other, key),
                f"a second real operator session enumerated {key} it did not "
                "create — the read resolved ownership from something other "
                "than the session account id")
            self.assertNotIn(rid, self._ids(ov_other, _PENDING_TO_SCOPED[key]),
                             (key, "leaked through the scoped list instead"))
        self.assertEqual(second["id"], second["id"])

        # Post-link, over HTTP: grant the Venue to the demo's active Season and
        # the Venue + its Rink move out of pending and into the scoped lists.
        ctx = self._overview(admin)
        season_ids = [s["id"] for s in ctx["seasons"]]
        self.assertTrue(season_ids, ctx)
        self._post(admin, f"seasons/{season_ids[0]}/venue-access",
                   {"venue_id": venue["id"]})
        linked = self._overview(admin)
        for key, scoped_key, rid in (
                ("pending_link_venues", "venues", venue["id"]),
                ("pending_link_rinks", "rinks", rink["id"])):
            self.assertNotIn(
                rid, self._ids(linked, key),
                f"{key} still offers a row that now holds a validated "
                "Program link")
            self.assertIn(
                rid, self._ids(linked, scoped_key),
                f"a newly-linked row left {key} without arriving in "
                f"{scoped_key}")
        # ...and the CEASING half holds for everyone, not just the creator:
        # the row is no longer offered on anybody's creator-ownership ticket.
        # The second operator resolves the same (single) demo Program, so it
        # now receives the Venue through the normal SCOPED list instead —
        # which is the point of the contract: once a record has a real Program
        # link it is that Program's data, governed by the active-tuple
        # ceiling, and no longer anyone's private pending row.
        ov_other = self._overview(other)
        self.assertNotIn(
            venue["id"], self._ids(ov_other, "pending_link_venues"),
            "a linked Venue is still being offered as somebody's pending-link "
            "row")
        self.assertIn(
            venue["id"], self._ids(ov_other, "venues"),
            "a Venue granted to the active Season did not reach a second "
            "operator authorized for that same Program through the scoped "
            "list")

    def test_http_a_deactivated_creator_cannot_come_back_and_releases_nothing(self):
        """The decided deactivation behavior, end to end: the creator's live
        session stops resolving, re-login is refused, and the rows are handed
        to nobody — not the deactivating admin, not a fresh account."""
        admin = self._login("admin")
        acct = self._make_operator(admin, "pl_leaver", "correct-horse-5",
                                   "arena_manager")
        leaver = self._login("pl_leaver", "correct-horse-5")
        venue = self._post(leaver, "venue", {"name": "HTTP-PL-Leaver-Venue"})
        org = self._post(leaver, "organization",
                         {"name": "HTTP-PL-Leaver-Org"})
        self.assertIn(venue["id"],
                      self._ids(self._overview(leaver), "pending_link_venues"))

        status, res = self._req(admin, "POST",
                                f"/api/accounts/{acct['id']}/active",
                                {"active": False})
        self.assertEqual(status, 200, res)
        self.assertFalse(res["active"], res)

        # The live session is dead...
        status, _ = self._req(leaver, "GET", "/api/v2/setup/overview")
        self.assertEqual(
            status, 401,
            "a deactivated account kept a working session, so its "
            "never-linked rows are still reachable by it")
        # ...and it cannot sign back in.
        fresh = self._client()
        status, body = self._req(fresh, "POST", "/api/auth/login",
                                 {"username": "pl_leaver",
                                  "password": "correct-horse-5"})
        self.assertIn(status, (401, 403),
                      "a deactivated account signed back in with its old "
                      f"credentials and regained its pending-link rows: "
                      f"{status} {body}")

        # Nobody inherits the rows: not the admin who deactivated it...
        ov_admin = self._overview(admin)
        for key, rid in (("pending_link_venues", venue["id"]),
                         ("pending_link_organizations", org["id"])):
            self.assertNotIn(
                rid, self._ids(ov_admin, key),
                f"deactivating an account released its {key} row to the "
                "admin who deactivated it")
            self.assertNotIn(rid, self._ids(ov_admin, _PENDING_TO_SCOPED[key]),
                             (key, "released through the scoped list"))
        # ...nor a brand-new account created afterwards.
        self._make_operator(admin, "pl_successor", "correct-horse-6",
                            "arena_manager")
        successor = self._login("pl_successor", "correct-horse-6")
        ov_successor = self._overview(successor)
        for key, rid in (("pending_link_venues", venue["id"]),
                         ("pending_link_organizations", org["id"])):
            self.assertNotIn(
                rid, self._ids(ov_successor, key),
                f"a newly-created account inherited a deactivated creator's "
                f"{key} row")

    def test_http_writing_under_a_guessed_parent_id_is_refused_outright(self):
        """The probe over real sessions and real authorization: an ARENA
        MANAGER — the identity the ruling names — tries to create children
        under another session's never-linked parent ids, and every one is
        REFUSED.

        This assertion was reversed in the #369 review round. It previously
        required only that the probe creates come back with the parent's NAME
        withheld; the review established that redaction was the wrong remedy,
        because withholding the name leaves two disclosures intact. Whether
        the create SUCCEEDS is itself an existence oracle over sequential ids
        (``venue_9`` accepted, ``venue_99`` not), and — the part no redaction
        can address — the write really happens: another creator's private
        setup graph silently grows a Rink, its Rink grows ice, its
        Organization grows a Venue, its Club grows an Official carrying a
        person's name. The remedy is that the write never lands at all, so
        that is what is asserted here now.
        """
        admin = self._login("admin")
        secret_venue = self._post(admin, "venue",
                                  {"name": "HTTP-PROBE-SECRET-VENUE"})
        secret_rink = self._post(admin, "rink",
                                 {"venue_id": secret_venue["id"],
                                  "name": "HTTP-PROBE-SECRET-RINK"})
        secret_org = self._post(admin, "organization",
                                {"name": "HTTP-PROBE-SECRET-ORG"})
        secret_club = self._post(admin, "club",
                                 {"name": "HTTP-PROBE-SECRET-CLUB"})

        # Arena Manager holds manage_arena AND manage_schedule, so all four
        # probe creates — Rink, IceSlot, Venue and Official — are reachable by
        # this one role, the Official/home-club one included. That is the
        # personal-data case the ruling singles out.
        arena = self._login("arena")
        before = self._overview(admin)
        slot_times = {"start_time": "2027-06-01T10:00:00+00:00",
                      "end_time": "2027-06-01T11:00:00+00:00"}
        # (route, body key naming the parent, the owner's real id, the noun
        # the facade uses when that kind of parent genuinely does not exist)
        probes = [
            ("official", "home_club_id", secret_club["id"], "Club",
             {"name": "probe-official"}),
            ("rink", "venue_id", secret_venue["id"], "Venue",
             {"name": "probe-rink"}),
            ("ice-slot", "rink_id", secret_rink["id"], "Rink", slot_times),
            ("venue", "organization_id", secret_org["id"], "Organization",
             {"name": "probe-venue"}),
        ]
        for entity, key, parent_id, label, rest in probes:
            status, resp = self._req(arena, "POST",
                                     f"/api/v2/setup/{entity}",
                                     {key: parent_id, **rest})
            self.assertEqual(
                status, 404,
                f"an Arena Manager wrote a {entity} into another creator's "
                f"private setup graph under {key}={parent_id}: {resp}")
            self.assertEqual(resp["error"]["message"],
                             f"{label} {parent_id} not found.", resp)
            # Identical to a parent id that never existed, so the refusal
            # cannot be used to probe which sequential ids are real. Compared
            # after substituting the id, which is the only field either
            # response is allowed to differ in (it echoes back the caller's
            # own input, never new information).
            ghost_id = f"{key[:-3]}_does_not_exist"
            ghost_status, ghost_resp = self._req(
                arena, "POST", f"/api/v2/setup/{entity}",
                {key: ghost_id, **rest})
            self.assertEqual(
                (ghost_status, ghost_resp),
                (status, {"error": {"code": resp["error"]["code"],
                                    "message": f"{label} {ghost_id} "
                                               "not found."}}),
                f"the inaccessible-{key} refusal is distinguishable from the "
                f"nonexistent one: {resp} vs {ghost_resp}")

        # Nothing landed: the owner's graph is byte-for-byte what it was.
        self.assertEqual(
            json.dumps(self._overview(admin), sort_keys=True),
            json.dumps(before, sort_keys=True),
            "a refused cross-creator write still mutated the owner's setup "
            "graph")

        # ...and no name leaked into the prober's own view either.
        blob = json.dumps(self._overview(arena))
        for name in ("HTTP-PROBE-SECRET-VENUE", "HTTP-PROBE-SECRET-RINK",
                     "HTTP-PROBE-SECRET-ORG", "HTTP-PROBE-SECRET-CLUB"):
            self.assertNotIn(
                name, blob,
                f"an Arena Manager read {name!r} back out of its own rows")

        # Positive control: the SAME creates succeed under parents this
        # caller made itself, so the refusals above are a scope decision and
        # not a blanket "Arena Managers cannot create".
        own_venue = self._post(arena, "venue", {"name": "arena-own-venue"})
        own_rink = self._post(arena, "rink", {"venue_id": own_venue["id"],
                                              "name": "arena-own-rink"})
        self._post(arena, "ice-slot", dict(rink_id=own_rink["id"],
                                           **slot_times))

    def test_http_the_demo_seed_is_attributed_to_a_real_account(self):
        """The ruling's demo-seed condition, asserted at the only layer that
        can prove it: the seeded data's ``actor_id`` must resolve to a REAL,
        active account — not a synthetic label like ``"league_admin"``, which
        is a ROLE name no account ever carries and which therefore made every
        seeded record with no Program link owned by nobody and visible to
        nobody. The seeded Officials with neither a home Club that has a Team
        nor a game assignment are exactly those records, and signing in as
        that real account must produce them."""
        store = self.srv.STATE.api.store
        account = store.get_user_account(DEMO_ADMIN_ACCOUNT_ID)
        self.assertIsNotNone(
            account,
            f"the demo seed attributes its records to "
            f"{DEMO_ADMIN_ACCOUNT_ID!r}, which resolves to NO account — a "
            "synthetic actor label owns nothing, so every never-linked "
            "seeded record is visible to nobody")
        self.assertTrue(account.active, account)

        created_actors = {a.actor_id for a in store.all_setup_audit()
                          if a.action.endswith("_created")
                          and a.entity_type in ("club", "organization",
                                                "official", "venue", "rink",
                                                "ice_slot")}
        self.assertTrue(created_actors, "no seeded create audit rows at all")
        for actor in created_actors:
            self.assertIsNotNone(
                store.get_user_account(actor),
                f"a seeded record is attributed to {actor!r}, which is not a "
                "real account id — attribute the seed to a real account "
                "(#367 ruling), never bypass the check for a synthetic label")

        # End to end: the seeded, never-linked Officials reach the real admin
        # session's own pending list. This is the demo-data regression the
        # ruling calls out — "a club with no teams" in the report, seeded
        # Officials with no club and no assignment in fact.
        admin = self._login("admin")
        ov = self._overview(admin)
        self.assertTrue(
            ov["pending_link_officials"],
            "the seeded never-linked Officials are visible to nobody: the "
            "demo seed's actor is not the signed-in admin's account id")
        # And they are exactly the never-linked Officials the SEED created —
        # computed from the store, restricted to this actor's own create
        # entries so a sibling test in this class (which shares one reset
        # store) creating its own Official cannot change the expected set.
        club_ids_with_teams = {t.club_id for t in store.all_teams()
                               if t.club_id}
        seeded_official_ids = {
            a.entity_id for a in store.all_setup_audit()
            if a.entity_type == "official"
            and a.action == "official_created"
            and a.actor_id == DEMO_ADMIN_ACCOUNT_ID}
        expected = {o.id for o in store.all_officials()
                    if o.id in seeded_official_ids
                    and o.home_club_id not in club_ids_with_teams
                    and not any(store.get_game(a.game_id) is not None
                                and store.get_game(a.game_id).season_id
                                for a in
                                store.assignments_for_official(o.id))}
        self.assertTrue(expected, "no never-linked seeded Officials at all")
        self.assertEqual(
            self._ids(ov, "pending_link_officials") & seeded_official_ids,
            expected,
            "the admin's pending-link Officials are not exactly the seeded "
            "never-linked ones")


if __name__ == "__main__":
    unittest.main()
