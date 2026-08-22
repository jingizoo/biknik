"""Substitute eligibility requires a LIVE participation spine (#205 review,
owner comment 5368386042 — "blocker 2").

WHAT WAS WRONG. The #205 cutover made ``RosterService.resolve_membership``
the one eligibility primitive, matched on the game's EXACT
``league_season_id``. That exactness is necessary and it is not sufficient.
The resolver validated only three things: the game names a LeagueSeason,
that row EXISTS, and the membership's own ``league_season_id``/``team_id``/
``status``. It never re-checked the participation the membership hangs off —
the Team's ACTIVE ``SeasonTeamRegistration``, the Team-League edge, the
Team/League/Season Program leg, or the membership's own DENORMALIZED
``season_id``. Every one of those was validated at WRITE time only
(``SetupService._assert_membership_program_spine`` /
``_assert_membership_spine_valid``), so a restored backup, a direct/bulk
writer, or a parent-mutation race left a membership row that still granted
participation after its parent participation had ENDED.

REPRODUCED, tri-store, on head 874c18e BEFORE the fix (captured transcript
in the PR body): build a valid HOME player/membership on the game's exact
LeagueSeason, set that HOME ``SeasonTeamRegistration.active = False`` at the
STORE — the restored/legacy/direct-writer state the membership spine guard
explicitly says read-time checks must contain — then resolve and enroll.
``resolve_membership`` returned the ACTIVE membership, ``team_for_game``
returned HOME, and ``enroll_substitute`` succeeded, writing an ENROLLED row,
an ``AuditLog`` row and a coach ``NotificationEvent``; ``offer``/``accept``
then completed the arc and wrote an ACCEPTED roster row plus feed and
delivery rows. Identical on Memory, SQLite AND PostgreSQL, for ACTIVE and
for AFFILIATE memberships, and identically for every other broken spine
edge.

WHAT THIS FILE PINS. The resolver now returns ONE
``GameMembershipContext`` — and only after validating membership/player
identity, the exact LeagueSeason AND the denormalized Season, the
participating Team, the Team-League-Season/Program spine, and a CURRENT
ACTIVE ``SeasonTeamRegistration``. That single context is threaded through
every decision, so team and position are never re-read independently, and a
BOUND game never falls back to permanent team/position when it fails.
Genuinely unbound games (exhibitions, legacy rows) keep the permanent
pointer exactly as before — ``UnboundGamesKeepThePermanentGate`` in
``test_substitute_membership_cutover.py`` is that half's regression and is
untouched by this fix.

THE MATRIX. {ACTIVE, AFFILIATE} membership x every spine edge x {Memory,
SQLite, PostgreSQL}, in BOTH commit orders (parent participation ends
BEFORE the substitute transition, and BETWEEN two of them), each asserting
that every private read (opportunity detail, candidate queue, addable pool,
block reason, opportunity list) and every mutation (enroll / offer / accept
/ Coach add / Coach add-to-roster) fails CLOSED with ZERO writes across all
FOUR write classes — substitute rows, roster rows, audit rows (``AuditLog``
AND ``SetupAuditLog``) and notification rows (``NotificationEvent``, the
``Notification`` feed AND ``NotificationDelivery``). A caught exception
alone is not a pass: a path that refuses but leaves an audit row is a FAIL.

INTACT-SPINE CONTROLS ride alongside every refusal so a guard that simply
refused everyone could not pass: with the spine untouched the same subject
enrolls, is offered, accepts, appears in both private lists and reads their
own opportunity detail.

A SKIP IS NOT A PASS. The PostgreSQL leg does not merely read
``TEST_DATABASE_URL`` and hope: ``_stores`` yields a real ``SqlStore`` and
:meth:`_assert_backend` asserts ``store.backend == "postgres"`` inside every
case before it runs, so a "tri-store" claim made here is checkable at
runtime. (A sibling blocker on this same PR was exactly a vacuous tri-store
claim: two classes advertised as tri-store silently ran Memory+SQLite only.)
"""

import copy
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import fresh_sql_store

from hockey_scheduler.domain import LeagueSeason, SubstituteStatus
from hockey_scheduler.domain.errors import NotEligibleError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

from test_substitute_membership_cutover import ADMIN, _Fixture


# ======================================================================
# the spine edges, each broken exactly the way a real writer breaks it
# ======================================================================

def _save(store, obj, kind):
    """Persist a mutated parent row. ``SqlStore`` writes need a transaction;
    ``InMemoryStore`` does not (its ``transaction()`` is a no-op but nesting
    is still not reentrant, so the branch is kept explicit)."""
    saver = getattr(store, "save_" + kind)
    if isinstance(store, SqlStore):
        with store.transaction():
            saver(obj)
    else:
        saver(obj)


def _registration(api, fx):
    return api.store.registration_for_team_in_league_season(
        fx["ls_id"], fx["home"])


def _break_registration_inactive(api, fx):
    """``SeasonTeamRegistration.active = False`` — the EXACT field
    ``unregister_team_from_season`` writes (``setup_service.py`` :2381),
    reached here at the store: the restored / legacy / direct-writer shape
    the membership spine guard says read-time checks must contain, and the
    owner's own repro."""
    reg = _registration(api, fx)
    reg.active = False
    _save(api.store, reg, "season_team_registration")
    return f"registration {reg.id}.active=False"


def _break_registration_missing(api, fx):
    """The registration row is GONE — what
    ``delete_season_team_registration`` (``setup_service.py`` :2389) leaves
    behind, and what any restore that lost the table leaves behind."""
    reg = _registration(api, fx)
    if isinstance(api.store, SqlStore):
        with api.store.transaction():
            api.store.delete_season_team_registration(reg.id)
    else:
        api.store.delete_season_team_registration(reg.id)
    return f"registration {reg.id} DELETED"


def _break_registration_moved(api, fx):
    """``SeasonTeamRegistration.league_season_id`` repointed at another
    LeagueSeason — the EXACT field ``assign_season_team_league``
    (``setup_service.py`` :1327) and ``transfer_team_to_league`` (:2317)
    rewrite. The Team's participation now lives in a DIFFERENT competition
    while the membership still names this one."""
    ls2 = fx["other_ls"]
    reg = _registration(api, fx)
    reg.league_season_id = ls2.id
    _save(api.store, reg, "season_team_registration")
    return f"registration {reg.id}.league_season_id -> {ls2.id}"


def _break_team_league_other(api, fx):
    """``Team.league_id`` names a SIBLING League — the Team-League
    permanence edge (#283 rule 7), the same one
    ``_assert_membership_spine_valid`` refuses at write time
    (``membership_league_mismatch``)."""
    team = api.store.get_team(fx["home"])
    team.league_id = fx["other_league"]["id"]
    _save(api.store, team, "team")
    return f"team.league_id -> {fx['other_league']['id']}"


def _break_team_league_missing(api, fx):
    """``Team.league_id`` is ABSENT. Missing is a violation, not an
    exemption — the falsy-skip ``missing_or_unequal`` exists to close."""
    team = api.store.get_team(fx["home"])
    team.league_id = None
    _save(api.store, team, "team")
    return "team.league_id -> None"


def _break_team_program(api, fx):
    """``Team.program_id`` names a sibling Program. Planted from a REAL
    second Program (``api.create_program``), never a bogus id: SQLite and
    PostgreSQL both hold a real foreign key here and refuse an id that
    resolves to no row."""
    team = api.store.get_team(fx["home"])
    team.program_id = fx["other_program"]["id"]
    _save(api.store, team, "team")
    return f"team.program_id -> {fx['other_program']['id']}"


def _break_league_program(api, fx):
    league = api.store.get_league(fx["league"]["id"])
    league.program_id = fx["other_program"]["id"]
    _save(api.store, league, "league")
    return f"league.program_id -> {fx['other_program']['id']}"


def _break_season_program(api, fx):
    season = api.store.get_season(fx["season"]["id"])
    season.program_id = fx["other_program"]["id"]
    _save(api.store, season, "season")
    return f"season.program_id -> {fx['other_program']['id']}"


def _break_membership_season(api, fx):
    """The membership's DENORMALIZED ``season_id`` names a REAL sibling
    Season, not the one its ``league_season_id`` resolves to.

    The column is service-enforced equal to ``LeagueSeason.season_id`` at
    BIRTH only (``create_season_roster_membership``, ``setup_service.py``
    :3020) and exists so migration 059's partial unique index
    ``ux_srm_active_player_season`` can hold "one ACTIVE membership per
    (player, Season)" without a join. Nothing re-checks it afterwards, and
    on SQL both columns are FK-constrained to EXISTING rows without ever
    being constrained to the SAME Season — so this shape is built from a
    real sibling Season, which is exactly why it is reachable."""
    m = api.store.get_season_roster_membership(fx["membership_id"])
    m.season_id = fx["other_season"]["id"]
    _save(api.store, m, "season_roster_membership")
    return f"membership.season_id -> {fx['other_season']['id']}"


def _break_registration_duplicated(api, fx):
    """TWO registration rows at the identical ``(team, league_season)``
    key. SQL's ``ux_team_league_season`` unique index (migration 035) makes
    this impossible to INSERT, so it is reachable on ``InMemoryStore``
    only — which enforces nothing, and whose bare lookup silently returns
    whichever row sorts first. ``exact_registration_or_conflict`` is the
    shared wrapper that turns that into an unconditional CLOSED answer, and
    this is the shape that proves the resolver uses the wrapper rather than
    the bare lookup."""
    reg = _registration(api, fx)
    twin = copy.copy(reg)
    twin.id = api.store.next_id("streg")
    twin.active = False
    api.store.add_season_team_registration(twin)
    return f"second registration {twin.id} at the same exact key"


def _break_team_missing(api, fx):
    """The participating ``Team`` row itself is gone while the membership
    survives. Reachable on ``InMemoryStore`` only — SQL holds a real
    foreign key from ``season_roster_memberships.team_id``."""
    del api.store.teams[fx["home"]]
    return f"team {fx['home']} row DELETED"


def _break_player_missing(api, fx):
    """The membership's ``Player`` row is gone — the identity leg
    ``_assert_membership_spine_valid`` refuses at write time
    (``membership_player_missing``). ``InMemoryStore`` only, same FK
    reason."""
    del api.store.players[fx["player"]["id"]]
    return f"player {fx['player']['id']} row DELETED"


def _break_season_missing(api, fx):
    """The Season the LeagueSeason names is gone. ``InMemoryStore`` only
    (``league_seasons.season_id`` is a real FK on SQL)."""
    del api.store.seasons[fx["season"]["id"]]
    return f"season {fx['season']['id']} row DELETED"


class _Edge:
    """One spine edge, the writer-faithful way to break it, and the
    resolver check that must catch it (named so falsifiability can target
    ONE check at a time)."""

    def __init__(self, name, apply, check, memory_only=False,
                 code="not_eligible"):
        self.name = name
        self.apply = apply
        self.check = check
        self.memory_only = memory_only
        # The error code the ELIGIBILITY-bearing mutations answer with. Every
        # participation edge is ``not_eligible``; the deleted-Player edge is
        # the one shape whose subject cannot be looked up at all, so its
        # refusal is ``not_found`` from ``_require_player`` — still closed,
        # still zero writes, just an earlier gate.
        self.code = code

    def __repr__(self):
        return f"<edge {self.name}>"


EDGES = (
    _Edge("registration.active=False", _break_registration_inactive,
          "team_not_registered"),
    _Edge("registration DELETED", _break_registration_missing,
          "team_not_registered"),
    _Edge("registration MOVED to another LeagueSeason",
          _break_registration_moved, "team_not_registered"),
    _Edge("registration DUPLICATED at the exact key",
          _break_registration_duplicated, "team_registration_conflict",
          memory_only=True),
    # NOTE the duplicated-key edge is why every case asserts the REASON and
    # not merely "closed": two legs correctly backstop each other there
    # (``exact_registration_or_conflict`` returns NO row on a conflict, so
    # the not-registered leg would close the gate even with the conflict leg
    # deleted). Falsifiability caught that: deleting the conflict leg left
    # every "is it closed?" assertion green. The reason distinguishes them.
    _Edge("team.league_id -> sibling League", _break_team_league_other,
          "membership_league_mismatch"),
    _Edge("team.league_id -> missing", _break_team_league_missing,
          "membership_league_mismatch"),
    _Edge("team.program_id -> sibling Program", _break_team_program,
          "membership_program_mismatch"),
    _Edge("league.program_id -> sibling Program", _break_league_program,
          "membership_program_mismatch"),
    _Edge("season.program_id -> sibling Program", _break_season_program,
          "membership_program_mismatch"),
    _Edge("membership.season_id -> sibling Season", _break_membership_season,
          "membership_denormalized_season_mismatch"),
    _Edge("Team row DELETED", _break_team_missing,
          "membership_team_missing", memory_only=True),
    # ``code=None``: these two shapes are caught by an EARLIER gate than the
    # eligibility one on some paths — ``_require_player`` answers
    # ``not_found`` for a deleted Player, and ``_guard_active_season``
    # answers ``not_found`` for a deleted Season — and by the spine gate on
    # others. Both are closed with zero writes either way, which is what is
    # asserted; pinning one code would pin the accident of which gate ran
    # first rather than the guarantee.
    _Edge("Player row DELETED", _break_player_missing,
          "membership_player_missing", memory_only=True, code=None),
    _Edge("Season row DELETED", _break_season_missing,
          "membership_season_missing", memory_only=True, code=None),
)

MEMBERSHIP_KINDS = ("active", "affiliate")


# ======================================================================
# the ONE tri-store harness
# ======================================================================

_PG_SKIP = (
    "PostgreSQL not configured (TEST_DATABASE_URL) — the #205 blocker-2 "
    "membership-spine gate was NOT exercised on PostgreSQL. A SKIP IS NOT A "
    "PASS: the registration/Team/League/Season reads behind the gate are "
    "real SQL here and the refusals must leave zero rows inside PostgreSQL "
    "transactions. Set TEST_DATABASE_URL (run_parallel.py --postgres does).")


class _SpineHarness(_Fixture):
    """Fixture + assertions, written ONCE and invoked verbatim by the
    Memory/SQLite class, the PostgreSQL class and the real-HTTP class, so no
    backend's assertions can drift away from another's."""

    # -- stores ----------------------------------------------------------
    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")
        url = os.environ.get("TEST_DATABASE_URL")
        if url:
            yield "postgres", fresh_sql_store(url)

    def _assert_backend(self, label, store):
        """PROVE the backend, never assume it. ``skipUnless`` on the env var
        proves only that a URL was SET."""
        if label == "postgres":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "postgres", store.backend)
        elif label == "sqlite":
            self.assertIsInstance(store, SqlStore, label)
            self.assertEqual(store.backend, "sqlite", store.backend)
        else:
            self.assertIsInstance(store, InMemoryStore, label)

    def _close(self, label, store):
        if isinstance(store, SqlStore):
            if label == "postgres":
                store.reset_schema()
            store.close()

    # -- fixture ---------------------------------------------------------
    def _spine_fixture(self, store, kind):
        """A LeagueSeason-bound REGULAR game (HOME vs AWAY) plus ONE subject
        player holding exactly ONE eligible membership on HOME, and the real
        sibling Program/Season/League rows every "disagreeing parent" shape
        has to be built from (SQL foreign keys refuse a bogus id)."""
        api, season, league, teams, game, ls_id = self._build(store)
        if kind == "active":
            player = self._player(api, teams["home"]["id"], "Subject",
                                  jersey=9)
            membership_id = self._stint_id(api, player["id"], ls_id)
        else:
            # AFFILIATE — the governed call-up exception — on HOME, while the
            # permanent pointer names THIRD. Built at the STORE so no parity
            # ACTIVE stint collides with the one-open-membership-per-
            # (player, LeagueSeason) rule (setup_service.py:3004).
            player = self._pointer_only_player(api, teams["third"]["id"],
                                               "Subject")
            m = self._membership(api, player["id"], ls_id,
                                 teams["home"]["id"], status="affiliate",
                                 jersey=71)
            membership_id = m["id"]
        league_row = api.store.get_league(league["id"])
        program_id = league_row.program_id
        org_id = api.store.get_program(program_id).operator_organization_id
        other_program = api.create_program(
            "Other Program", operator_organization_id=org_id, actor_id=ADMIN)
        assert "error" not in other_program, other_program
        other_season = api.create_season(program_id, "Spring 2027",
                                         actor_id=ADMIN)
        assert "error" not in other_season, other_season
        other_league = api.create_league(season["id"], "House",
                                         actor_id=ADMIN)
        assert "error" not in other_league, other_league
        # The League's OTHER LeagueSeason (same League, the sibling Season) —
        # a real destination for the "registration moved away" shape.
        other_ls = api.store.league_season_for(league["id"],
                                               other_season["id"])
        if other_ls is None:
            other_ls = LeagueSeason(id=api.store.next_id("ls"),
                                    league_id=league["id"],
                                    season_id=other_season["id"])
            _save(api.store, other_ls, "league_season")
        return {
            "api": api, "season": season, "league": league, "teams": teams,
            "game": game, "ls_id": ls_id, "player": player,
            "membership_id": membership_id, "kind": kind,
            # The live Player OBJECT as of fixture time. The deleted-Player
            # edge needs it: after the row is gone ``get_player`` answers
            # None, and a caller holding a stale object is exactly the shape
            # the resolver's identity leg exists to refuse.
            "player_obj": api.store.get_player(player["id"]),
            "home": teams["home"]["id"], "away": teams["away"]["id"],
            "third": teams["third"]["id"],
            "other_program": other_program, "other_season": other_season,
            "other_league": other_league, "other_ls": other_ls,
        }

    def _cases(self, edges=EDGES, kinds=MEMBERSHIP_KINDS):
        """``(label, kind, edge, fx)`` for every REACHABLE combination, with
        the backend PROVEN before any case body runs and a wiped store in
        front of each one."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in kinds:
                    for edge in edges:
                        if edge.memory_only and label != "memory":
                            continue
                        store.clear_all_data()
                        yield label, kind, edge, self._spine_fixture(store,
                                                                     kind)
            finally:
                self._close(label, store)

    def _assert_matrix_ran(self, ran):
        """The loop is never silently empty, and PostgreSQL is never silently
        absent when it was configured — the exact failure a vacuous tri-store
        claim hides."""
        backends = {b for b, _kind, _edge in ran}
        expected = {"memory", "sqlite"}
        if os.environ.get("TEST_DATABASE_URL"):
            expected.add("postgres")
        self.assertEqual(backends, expected, sorted(backends))
        for backend in expected:
            edges = {e for b, _k, e in ran if b == backend}
            reachable = {e.name for e in EDGES
                         if backend == "memory" or not e.memory_only}
            self.assertEqual(edges, reachable, (backend, sorted(edges)))
            kinds = {k for b, k, _e in ran if b == backend}
            self.assertEqual(kinds, set(MEMBERSHIP_KINDS), (backend, kinds))

    # -- write surfaces --------------------------------------------------
    def _writes(self, api, game_id):
        """ALL FOUR write classes the owner names, as comparable values:
        substitute rows, roster rows, audit rows (the game ``AuditLog`` AND
        the global ``SetupAuditLog``) and notification rows (the game
        ``NotificationEvent`` log, the ``Notification`` feed AND every
        ``NotificationDelivery`` fanned out from it)."""
        store = api.store
        return {
            "substitutes": sorted(
                (s.id, s.player_id, s.status.value)
                for s in store.substitutes_for_game(game_id)),
            "roster": sorted(
                (e.id, e.player_id, e.status.value)
                for e in store.roster_for_game(game_id)),
            "audit": sorted(
                (a.id, a.action.value) for a in store.audit_for_game(game_id)),
            "setup_audit": len(store.all_setup_audit()),
            "notification_events": sorted(
                (n.id, n.type.value)
                for n in store.notifications_for_game(game_id)),
            "notification_feed": sorted(
                (n.id, n.kind.value, n.audience_ref or "")
                for n in store.all_notifications_feed()),
            "deliveries": sorted(d.id for d in
                                 store.all_notification_deliveries()),
        }

    # -- the closed-gate assertions --------------------------------------
    def _assert_reads_closed(self, fx, label):
        api, r = fx["api"], fx["api"].roster
        store = api.store
        g = store.get_game(fx["game"]["id"])
        pid = fx["player"]["id"]
        # A STALE Player object when the row itself is gone — the caller
        # shape the identity leg refuses. Without it the deleted-Player edge
        # would never reach the resolver at all and its check would be
        # untestable (falsifiability caught exactly that).
        p = store.get_player(pid) or fx["player_obj"]

        # The resolver, its reason, and everything derived from it.
        self.assertIsNone(r.resolve_membership_context(g, p), label)
        self.assertIsNone(r.resolve_membership(g, p), label)
        self.assertIsNone(r.team_for_game(g, p), label)
        # A BOUND game must NOT fall back to the permanent position.
        with self.assertRaises(NotEligibleError, msg=label):
            r.position_for_game(g, p)
        self.assertNotIn(pid, r.resolve_membership_contexts_for_game(g), label)

        # Private reads.
        self.assertEqual(
            r.substitute_block_reason(pid, g.id),
            # The deleted-Player edge is caught by block_reason's own
            # identity check first; every other edge reaches the spine gate.
            "Player not found." if store.get_player(pid) is None
            else "You are not on a team in this game.", label)
        self.assertEqual(
            [x.id for x in r.list_substitute_opportunities(pid)], [], label)
        self.assertNotIn(
            pid, [row["player_id"]
                  for row in r.list_addable_players(g.id, fx["home"])], label)
        self.assertNotIn(
            pid, [row["player_id"]
                  for row in r.list_substitute_candidates(g.id, fx["home"])],
            label)
        detail = api.get_substitute_opportunity(pid, g.id)
        self.assertIn("error", detail, (label, detail))
        self.assertEqual(detail["error"]["code"], "not_found", (label, detail))

    def _assert_spine_reason(self, fx, label, expected):
        """WHICH edge the resolver says broke. Asserted per case so each
        spine leg is falsifiable ON ITS OWN — several legs correctly backstop
        one another, and "is it closed?" alone cannot tell them apart."""
        api = fx["api"]
        g = api.store.get_game(fx["game"]["id"])
        p = api.store.get_player(fx["player"]["id"]) or fx["player_obj"]
        self.assertEqual(
            api.roster.membership_spine_break_reason(g, p), expected, label)

    def _assert_reads_open(self, fx, label):
        """The mirror of :meth:`_assert_reads_closed` — used as the control
        beside a refusal that must NOT have changed anything."""
        api, r = fx["api"], fx["api"].roster
        g = api.store.get_game(fx["game"]["id"])
        p = api.store.get_player(fx["player"]["id"])
        pid = fx["player"]["id"]
        ctx = r.resolve_membership_context(g, p)
        self.assertIsNotNone(ctx, label)
        self.assertIsNone(r.membership_spine_break_reason(g, p), label)
        self.assertEqual(ctx.team_id, fx["home"], label)
        self.assertIsNone(r.substitute_block_reason(pid, g.id), label)
        self.assertIn(
            pid, [row["player_id"]
                  for row in r.list_addable_players(g.id, fx["home"])], label)
        self.assertNotIn("error", api.get_substitute_opportunity(pid, g.id),
                         label)

    _MUTATIONS = {
        "enroll": lambda api, gid, pid: api.enroll_substitute(gid, pid, ADMIN),
        "coach_add": lambda api, gid, pid: api.add_substitute_candidate(
            gid, pid, ADMIN),
        "offer": lambda api, gid, pid: api.offer_substitute(gid, pid, ADMIN),
        "accept": lambda api, gid, pid: api.accept_substitute(gid, pid, ADMIN),
        "add_to_roster": lambda api, gid, pid: api.add_substitute_to_roster(
            gid, pid, ADMIN),
    }

    def _assert_mutations_closed(self, fx, label, names, code=None):
        """Each named mutation REFUSES and writes NOTHING — the write
        surfaces are snapshotted and compared around EACH call, so a path
        that refuses but leaves an audit or notification row is caught
        individually rather than hidden behind a later one.

        ``code`` additionally pins WHY it refused, so an incidental state
        error (no enrollment yet, wrong status) can never be mistaken for the
        eligibility gate doing its job."""
        api, gid, pid = fx["api"], fx["game"]["id"], fx["player"]["id"]
        for name in names:
            before = self._writes(api, gid)
            res = self._MUTATIONS[name](api, gid, pid)
            self.assertIn("error", res, (label, name, res))
            if code is not None:
                self.assertEqual(res["error"]["code"], code,
                                 (label, name, res))
            self.assertEqual(
                self._writes(api, gid), before,
                (label, name, "a fail-closed path wrote something"))



# ======================================================================
# order 1 — the parent participation ends BEFORE any transition
# ======================================================================

class BrokenSpineClosesEveryReadAndTransition(_SpineHarness,
                                              unittest.TestCase):
    """COMMIT ORDER 1: the registration/Team/League/Season/denormalized-Season
    edge breaks FIRST, then the substitute surfaces are exercised.

    ``registration.active=False`` is exactly what ``unregister_team_from_
    season`` writes and ``registration.league_season_id`` is exactly what
    ``assign_season_team_league``/``transfer_team_to_league`` write, so the
    unregister-vs-transition and move-vs-transition orderings the owner asks
    for are the first three rows of this matrix (order 2 is the class
    below)."""

    def test_every_broken_edge_closes_every_read_and_mutation(self):
        ran = []
        for label, kind, edge, fx in self._cases():
            case = (label, kind, edge.name)
            with self.subTest(backend=label, membership=kind, edge=edge.name):
                note = edge.apply(fx["api"], fx)
                self.assertTrue(note, case)
                self._assert_spine_reason(fx, case, edge.check)
                self._assert_reads_closed(fx, case)
                # enroll and the Coach-add wrapper refuse for the ELIGIBILITY
                # reason, not incidentally.
                self._assert_mutations_closed(
                    fx, case, ("enroll", "coach_add"), code=edge.code)
                # offer/accept/add-to-roster have no enrollment to act on
                # here; they must still write nothing. Order 2 below reaches
                # them with a live enrollment, which is where the spine
                # itself has to close them.
                self._assert_mutations_closed(
                    fx, case, ("offer", "accept", "add_to_roster"))
            ran.append(case)
        self._assert_matrix_ran(ran)


# ======================================================================
# order 2 — the parent participation ends BETWEEN two transitions
# ======================================================================

class SpineEndingMidWorkflowClosesTheRest(_SpineHarness, unittest.TestCase):
    """COMMIT ORDER 2: the substitute workflow starts on an INTACT spine and
    the parent participation ends underneath it.

    The enrollment (and, in the second test, the offer) is the intact-spine
    control: it proves the gate is not simply refusing everyone, and it is
    the precondition that lets ``offer``/``accept``/``add_to_roster`` be
    reached at all — which is where the spine, not an incidental state
    error, has to close them."""

    def test_break_after_enroll_closes_offer_and_the_coach_overrides(self):
        ran = []
        for label, kind, edge, fx in self._cases():
            case = (label, kind, edge.name)
            with self.subTest(backend=label, membership=kind, edge=edge.name):
                api, gid, pid = fx["api"], fx["game"]["id"], fx["player"]["id"]
                enrolled = api.enroll_substitute(gid, pid, ADMIN)
                self.assertNotIn("error", enrolled, (case, enrolled))
                self.assertEqual(enrolled["status"], "enrolled", case)
                self.assertIn(
                    pid, [r["player_id"] for r in
                          api.roster.list_substitute_candidates(gid,
                                                                fx["home"])],
                    case)

                note = edge.apply(api, fx)
                self.assertTrue(note, case)

                self._assert_spine_reason(fx, case, edge.check)
                self._assert_reads_closed(fx, case)
                self._assert_mutations_closed(
                    fx, case, ("offer", "add_to_roster", "enroll"),
                    code=edge.code)
                # The enrollment itself is untouched history — the refusals
                # wrote nothing, in either direction.
                row = api.store.substitute_for_player(gid, pid)
                self.assertEqual(row.status, SubstituteStatus.ENROLLED, case)
            ran.append(case)
        self._assert_matrix_ran(ran)

    def test_break_after_offer_closes_accept_while_decline_still_commits(self):
        """``accept`` REVALIDATES eligibility and seats a body, so it fails
        closed. ``decline`` deliberately does NOT: it is the terminal
        response to an already-issued offer, addressed to the offer OWNER
        from ``SubstituteEnrollment.team_id`` (#205 blocker 3, migration
        060). A player's terminal act is never undone by a parent mutation
        they had no part in — asserted here so a later "make everything fail
        closed" pass cannot quietly regress blocker 3."""
        ran = []
        for label, kind, edge, fx in self._cases():
            case = (label, kind, edge.name)
            with self.subTest(backend=label, membership=kind, edge=edge.name):
                api, gid, pid = fx["api"], fx["game"]["id"], fx["player"]["id"]
                self.assertNotIn(
                    "error", api.enroll_substitute(gid, pid, ADMIN), case)
                offered = api.offer_substitute(gid, pid, ADMIN)
                self.assertNotIn("error", offered, (case, offered))
                self.assertEqual(
                    api.store.substitute_for_player(gid, pid).team_id,
                    fx["home"], case)

                note = edge.apply(api, fx)
                self.assertTrue(note, case)

                self._assert_spine_reason(fx, case, edge.check)
                self._assert_reads_closed(fx, case)
                self._assert_mutations_closed(
                    fx, case, ("accept", "add_to_roster"), code=edge.code)
                self.assertEqual(
                    api.store.substitute_for_player(gid, pid).status,
                    SubstituteStatus.OFFERED, case)

                if edge.name == "Season row DELETED":
                    # The ONE shape decline cannot survive: it destroys the
                    # Season ``_guard_active_season`` judges the game
                    # against, so the refusal happens before the offer-owner
                    # rule is ever reached. Still closed, just unable to
                    # demonstrate the blocker-3 commit-anyway contract that
                    # every other edge below does.
                    self.assertIn(
                        "error", api.decline_substitute(gid, pid, ADMIN),
                        case)
                    ran.append(case)
                    continue
                # NOTE the deleted-Player edge is NOT excepted here: decline
                # reads the offer's own snapshotted ``team_id`` and never
                # looks the Player up, so it commits and reaches the offer
                # owner even then — exactly the blocker-3 contract, and a
                # useful proof that the new spine gate did not creep into
                # the one path that must stay permissive.
                res = api.decline_substitute(gid, pid, ADMIN)
                self.assertNotIn("error", res, (case, res))
                self.assertEqual(res["status"], "declined", case)
                row = api.store.substitute_for_player(gid, pid)
                self.assertEqual(row.status, SubstituteStatus.DECLINED, case)
                self.assertEqual(row.team_id, fx["home"], case)
                self.assertIn(
                    ("substitute_declined", fx["home"]),
                    [(n.kind.value, n.audience_ref)
                     for n in api.store.all_notifications_feed()], case)
            ran.append(case)
        self._assert_matrix_ran(ran)


# ======================================================================
# the intact-spine control
# ======================================================================

class IntactSpineStillResolvesOneCoherentContext(_SpineHarness,
                                                 unittest.TestCase):
    """A guard that refused everything would pass every refusal above. With
    the spine untouched the SAME subject resolves to ONE coherent context and
    completes the whole arc — and the context's own rows are asserted, so
    "coherent" means the resolved LeagueSeason, Season, Team and ACTIVE
    registration, not merely a non-None answer."""

    def test_untouched_spine_resolves_and_completes_the_arc(self):
        ran = []
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in MEMBERSHIP_KINDS:
                    with self.subTest(backend=label, membership=kind):
                        store.clear_all_data()
                        fx = self._spine_fixture(store, kind)
                        api = fx["api"]
                        gid, pid = fx["game"]["id"], fx["player"]["id"]
                        g = api.store.get_game(gid)
                        p = api.store.get_player(pid)
                        ctx = api.roster.resolve_membership_context(g, p)
                        self.assertIsNotNone(ctx, (label, kind))
                        self.assertEqual(ctx.team_id, fx["home"], (label, kind))
                        self.assertEqual(ctx.membership.id,
                                         fx["membership_id"], (label, kind))
                        self.assertEqual(ctx.league_season.id, fx["ls_id"],
                                         (label, kind))
                        self.assertEqual(ctx.season.id, fx["season"]["id"],
                                         (label, kind))
                        self.assertEqual(ctx.team.id, fx["home"], (label, kind))
                        self.assertTrue(ctx.registration.active, (label, kind))
                        self.assertEqual(ctx.registration.league_season_id,
                                         fx["ls_id"], (label, kind))
                        # Team AND position come from that ONE context.
                        self.assertEqual(api.roster.team_for_game(g, p),
                                         ctx.team_id, (label, kind))
                        self.assertEqual(api.roster.position_for_game(g, p),
                                         ctx.position, (label, kind))
                        self.assertIsNone(
                            api.roster.substitute_block_reason(pid, gid),
                            (label, kind))
                        self.assertIn(
                            pid, [r["player_id"] for r in
                                  api.roster.list_addable_players(
                                      gid, fx["home"])], (label, kind))
                        detail = api.get_substitute_opportunity(pid, gid)
                        self.assertNotIn("error", detail, (label, kind, detail))
                        self.assertNotIn(
                            "error", api.enroll_substitute(gid, pid, ADMIN),
                            (label, kind))
                        self.assertIn(
                            pid, [r["player_id"] for r in
                                  api.roster.list_substitute_candidates(
                                      gid, fx["home"])], (label, kind))
                        self.assertNotIn(
                            "error", api.offer_substitute(gid, pid, ADMIN),
                            (label, kind))
                        entry = api.accept_substitute(gid, pid, ADMIN)
                        self.assertNotIn("error", entry, (label, kind, entry))
                        self.assertEqual(
                            api.store.substitute_for_player(gid, pid).status,
                            SubstituteStatus.ACCEPTED, (label, kind))
                        self.assertIn(
                            pid, [e.player_id for e in
                                  api.store.roster_for_game(gid)],
                            (label, kind))
                        ran.append((label, kind))
            finally:
                self._close(label, store)
        self.assertEqual(
            len({b for b, _ in ran}),
            3 if os.environ.get("TEST_DATABASE_URL") else 2, ran)


# ======================================================================
# the GOVERNED routes agree with the read-time gate
# ======================================================================

class GovernedRegistrationMutationsAgreeWithTheGate(_SpineHarness,
                                                    unittest.TestCase):
    """The write side and the read side must answer the same question.

    ``unregister_team_from_season`` REFUSES while a live membership exists
    (``setup_service.py`` :2370), which is why the fail-closed matrix above
    plants the ``active=False`` state at the store — the restored / legacy /
    direct-writer route the guard's own docstring says read-time checks must
    contain. This class pins both halves so neither can quietly drift:
    the write guard still blocks, and once participation legitimately ends
    the read gate closes too.

    ``assign_season_team_league`` is HTTP-exposed
    (``web/server.py`` :5218 -> ``api/service.py`` :11428) and its protection
    against stranding a live membership is INDIRECT — it rests on #283 rule 7
    (``team_league_mismatch``), not on a membership check of its own. That is
    pinned here too, so if rule 7 is ever relaxed the missing membership guard
    becomes visible rather than silently exploitable."""

    def test_unregister_refuses_while_a_live_membership_exists(self):
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in MEMBERSHIP_KINDS:
                    with self.subTest(backend=label, membership=kind):
                        store.clear_all_data()
                        fx = self._spine_fixture(store, kind)
                        api = fx["api"]
                        reg = _registration(api, fx)
                        res = api.unregister_team_from_season(reg.id,
                                                              actor_id=ADMIN)
                        self.assertIn("error", res, (label, kind, res))
                        self.assertTrue(
                            _registration(api, fx).active, (label, kind))
                        self._assert_reads_open(fx, (label, kind))
            finally:
                self._close(label, store)

    def test_the_governed_unregister_cannot_reach_the_dangerous_state(self):
        """WHY the matrix above plants ``active=False`` AT THE STORE.

        The governed route refuses TWICE over, in order: while the Team still
        has a scheduled game in the Season (``team_has_scheduled_games``) and,
        once that is out of the way, while a live membership exists
        (``team_has_live_memberships``). A game and a live membership are
        exactly the two things that have to be present for the substitute
        surfaces to mean anything — so this route CANNOT produce the state
        under test, and the restored / legacy / direct-writer route is the
        only one that can. That is precisely what the membership spine guard
        says read-time checks must contain, and it is why the read-time gate
        is not redundant with the write-time one.

        Both refusals are pinned so a future relaxation of either guard shows
        up here rather than silently widening the reachable state space."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                for kind in MEMBERSHIP_KINDS:
                    with self.subTest(backend=label, membership=kind):
                        store.clear_all_data()
                        fx = self._spine_fixture(store, kind)
                        api = fx["api"]
                        reg_id = _registration(api, fx).id
                        res = api.unregister_team_from_season(reg_id,
                                                              actor_id=ADMIN)
                        self.assertEqual(res["error"]["details"]["reason"],
                                         "team_has_scheduled_games",
                                         (label, kind, res))
                        # Take the game out of the way; the LIVE membership
                        # guard is the next refusal.
                        api.cancel_game(fx["game"]["id"], actor_id=ADMIN)
                        res = api.unregister_team_from_season(reg_id,
                                                              actor_id=ADMIN)
                        self.assertEqual(res["error"]["details"]["reason"],
                                         "team_has_live_memberships",
                                         (label, kind, res))
                        self.assertTrue(
                            _registration(api, fx).active, (label, kind))
            finally:
                self._close(label, store)

    def test_assign_league_still_refuses_a_sibling_league_move(self):
        """Rule 7 is the ONLY thing standing between
        ``assign_season_team_league`` and a stranded live membership: the
        Team's permanent League must equal the destination. Pinned so the
        indirect protection is visible."""
        for label, store in self._stores():
            try:
                self._assert_backend(label, store)
                with self.subTest(backend=label):
                    store.clear_all_data()
                    fx = self._spine_fixture(store, "active")
                    api = fx["api"]
                    reg = _registration(api, fx)
                    res = api.assign_season_team_league(
                        reg.id, fx["other_league"]["id"], actor_id=ADMIN)
                    self.assertIn("error", res, (label, res))
                    self.assertEqual(res["error"]["details"]["reason"],
                                     "team_league_mismatch", (label, res))
                    self.assertEqual(_registration(api, fx).league_season_id,
                                     fx["ls_id"], label)
            finally:
                self._close(label, store)


# ======================================================================
# no shape is silently unreachable
# ======================================================================

class NoSpineShapeIsSilentlyUnreachable(_SpineHarness, unittest.TestCase):
    """Four shapes are marked ``memory_only`` because a real foreign key or
    unique index makes them impossible to plant on SQLite/PostgreSQL. That
    claim is PROVEN here rather than asserted in prose: on the SQL stores the
    plant must RAISE (or leave the row untouched), never silently succeed —
    a shape that quietly became reachable would otherwise slip out of the
    matrix above without anyone noticing."""

    def test_memory_only_shapes_are_refused_by_the_sql_stores(self):
        checked = []
        for label, store in self._stores():
            if label == "memory":
                continue
            try:
                self._assert_backend(label, store)
                for edge in (e for e in EDGES if e.memory_only):
                    with self.subTest(backend=label, edge=edge.name):
                        store.clear_all_data()
                        fx = self._spine_fixture(store, "active")
                        try:
                            edge.apply(fx["api"], fx)
                        except Exception:
                            checked.append((label, edge.name, "refused"))
                            continue
                        # No raise: then the shape must still not have taken
                        # effect — otherwise it is reachable here and belongs
                        # in the main matrix.
                        p = fx["api"].store.get_player(fx["player"]["id"])
                        g = fx["api"].store.get_game(fx["game"]["id"])
                        self.assertIsNotNone(
                            fx["api"].roster.resolve_membership_context(g, p),
                            (label, edge.name,
                             "this shape is REACHABLE on this store — move it "
                             "out of memory_only and into the matrix"))
                        checked.append((label, edge.name, "no-op"))
            finally:
                self._close(label, store)
        self.assertTrue(checked)


# ======================================================================
# the same gate through REAL dispatched HTTP requests
# ======================================================================

class SpineGateOverHttp(unittest.TestCase):
    """The fail-closed gate exercised through REAL requests against
    ``web/server.py`` — a real socket, real sessions, real role scoping —
    not the facade in isolation. Covers both HTTP-exposed halves: the
    signed-in PLAYER's own opportunity/enroll routes and the COACH's
    outreach queue / addable pool / add-candidate route.

    Builds its fixture on the live demo server state (``srv.STATE``) via the
    setup facade so the game under test carries a genuine
    ``league_season_id`` — the existing HTTP substitute fixtures in
    ``test_substitute_opportunity.py`` build games directly at the store with
    no League binding at all and never reach this branch."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        harness = _SpineHarness()
        self.fx = harness._spine_fixture(self.api.store, "active")
        self.pid = self.fx["player"]["id"]
        self.gid = self.fx["game"]["id"]
        self.api.accounts.create_account(
            "spineplayer", "demo", "player",
            scope={"team_id": self.fx["home"], "player_id": self.pid},
            actor_id="test_seed")
        self.api.accounts.create_account(
            "spinecoach", "demo", "coach",
            scope={"team_id": self.fx["home"]}, actor_id="test_seed")
        self.api.accounts.create_account(
            "spineadmin", "demo", "league_admin", actor_id="test_seed")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _login(self, username):
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": username,
                             "password": "demo"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with opener.open(req) as r:
            self.assertEqual(r.status, 200)
        return opener

    def _req(self, opener, path, method="GET", body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body or {}).encode() if method == "POST" else None,
            method=method,
            headers={"Content-Type": "application/json"} if method == "POST"
            else {})
        try:
            with opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def _writes(self):
        return _SpineHarness()._writes(self.api, self.gid)

    def test_player_routes_open_while_the_spine_holds(self):
        """The control: with the registration intact the same routes work,
        so the refusals below are the SPINE closing and not the fixture."""
        c = self._login("spineplayer")
        status, d = self._req(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["position_needed"], "skater", d)
        status, d = self._req(
            c, f"/api/me/substitute-opportunities/{self.gid}/enroll",
            method="POST")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "enrolled", d)

    def test_player_routes_fail_closed_after_the_registration_ends(self):
        c = self._login("spineplayer")
        _break_registration_inactive(self.api, self.fx)
        before = self._writes()
        status, d = self._req(c, f"/api/me/substitute-opportunities/{self.gid}")
        self.assertEqual(status, 404, d)
        status, d = self._req(
            c, f"/api/me/substitute-opportunities/{self.gid}/enroll",
            method="POST")
        self.assertNotEqual(status, 200, d)
        self.assertEqual(d["error"]["code"], "not_eligible", d)
        self.assertEqual(self._writes(), before,
                         "the HTTP refusal wrote something")
        status, home = self._req(c, "/api/me/player-home")
        self.assertEqual(status, 200, home)
        self.assertEqual(
            [o["game_id"] for o in home["substitute_opportunities"]], [], home)

    def test_coach_routes_fail_closed_after_the_registration_ends(self):
        coach = self._login("spinecoach")
        # Control first: the coach can see and add this player.
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitute-addable")
        self.assertEqual(status, 200, d)
        self.assertIn(self.pid, [r["player_id"] for r in d["addable"]], d)

        _break_registration_inactive(self.api, self.fx)
        before = self._writes()
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitute-addable")
        self.assertEqual(status, 200, d)
        self.assertNotIn(self.pid, [r["player_id"] for r in d["addable"]], d)
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitute-candidates")
        self.assertEqual(status, 200, d)
        self.assertNotIn(self.pid,
                         [r["player_id"] for r in d["candidates"]], d)
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitutes/add-candidate",
            method="POST", body={"player_id": self.pid})
        self.assertNotEqual(status, 200, d)
        # Closed one gate EARLIER than the service: ``web/scope.py``'s coach
        # gate (:126) resolves the target player's team through THE SAME
        # ``team_for_game`` resolver, which now answers ``None``, so the
        # coach no longer owns this player for this game. Fail-closed either
        # way; the code names which gate caught it.
        self.assertEqual(d["error"]["code"], "forbidden", d)
        self.assertEqual(self._writes(), before,
                         "the HTTP refusal wrote something")
        # And the service gate underneath it says the same thing, reached
        # here through an OPERATOR session that the coach scope does not
        # constrain.
        admin = self._login("spineadmin")
        status, d = self._req(
            admin, f"/api/games/{self.gid}/substitutes/add-candidate",
            method="POST", body={"player_id": self.pid})
        self.assertNotEqual(status, 200, d)
        self.assertEqual(d["error"]["code"], "not_eligible", d)
        self.assertEqual(self._writes(), before,
                         "the HTTP refusal wrote something")

    def test_offer_and_accept_over_http_close_mid_workflow(self):
        """COMMIT ORDER 2 through real HTTP: enroll and offer land on an
        intact spine, the registration then ends, and accept fails closed
        with zero writes."""
        coach = self._login("spinecoach")
        player = self._login("spineplayer")
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitutes/add-candidate",
            method="POST", body={"player_id": self.pid})
        self.assertEqual(status, 200, d)
        status, d = self._req(
            coach, f"/api/games/{self.gid}/substitutes/{self.pid}/offer",
            method="POST")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["status"], "offered", d)

        _break_registration_inactive(self.api, self.fx)
        before = self._writes()
        status, d = self._req(
            player,
            f"/api/me/substitute-opportunities/{self.gid}/accept-offer",
            method="POST")
        self.assertNotEqual(status, 200, d)
        self.assertEqual(d["error"]["code"], "not_eligible", d)
        self.assertEqual(self._writes(), before,
                         "the HTTP refusal wrote something")
        self.assertEqual(
            self.api.store.substitute_for_player(self.gid, self.pid).status,
            SubstituteStatus.OFFERED)
