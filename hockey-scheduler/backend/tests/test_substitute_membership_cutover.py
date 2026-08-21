"""Substitute eligibility resolves through SeasonRosterMembership (#205).

THE CUTOVER THIS FILE PROVES. Before it, every substitute surface answered
"is this player on a team in this game?" from the PERMANENT ``player.team_id``
pointer — the one-player-one-team-forever coupling #205 abolishes. After it,
a Game bound to a LeagueSeason resolves that question through
``game.league_season_id -> Season -> SeasonRosterMembership``: an ACTIVE
membership (or AFFILIATE — the governed call-up exception the #205 model
defines) with one of the game's two teams in that Season grants eligibility;
nothing else does. Concretely, both directions:

  * a player whose permanent ``team_id`` matches the game's team but who has
    NO eligible membership in the game's Season is REJECTED (pre-cutover the
    permanent pointer let them through — the demonstrated defect); and
  * a player whose permanent ``team_id`` points elsewhere but who holds an
    ACTIVE membership with the game's team is ELIGIBLE (pre-cutover the
    permanent pointer shut them out — a mid-season transfer recorded as
    membership was invisible).

FAIL-CLOSED BOUNDARIES KEPT. Cross-boundary substitution stays CLOSED: an
eligible membership with a THIRD team (same Season, not in the game) grants
nothing — #287 open question 4 (who may substitute across League/Division
boundaries) is an unruled owner question, so the gate keeps today's posture.
applicant/inactive/injured memberships hold no current participation and
terminal rows are immutable history: none of them grants eligibility. A
bound game whose LeagueSeason row dangles resolves NOBODY rather than
falling back to permanent ownership.

THE PARITY DUAL-WRITE. Eligibility now reads memberships, so the two
service edges that used to make a player eligible implicitly must WRITE the
stint explicitly: ``add_player`` (an active player joining a team) and
``register_team_for_season`` (a team entering a Season with its players)
each open the ACTIVE memberships migration 059's backfill would have
implied — same rule, applied at create time. The seasonal record always
wins where it already says something (an open stint, or a recorded
transfer elsewhere in the Season, is never overwritten). Store-level bulk
imports and ``assign_player_team`` deliberately do NOT mirror: imports are
their own later cutover slice, and a permanent-pointer move must not
silently re-scope seasonal eligibility — that is the governed transfer
workflow's job. ``_pointer_only_player`` below models exactly that
import-shaped row.

WHAT DOES NOT CHANGE. A game with NO LeagueSeason binding (exhibitions by
design — ``Game.league_season_id`` is NULL for them — and legacy unbound
rows, including every game the pre-#283 tests build directly) has no Season
to resolve memberships against, so the permanent pointer remains its only
source, byte-for-byte today's behavior. The ENROLLED -> OFFERED ->
ACCEPTED/DECLINED/EXPIRED/WITHDRAWN state machine, its audits and its
notifications are untouched — only the eligibility source moved
(test_substitute_workflow/outreach/opportunity.py continue to pass
unchanged on the permanent-pointer path).

TRI-STORE. Memory + SQLite in every contract test; the PostgreSQL class
runs the full arc against a real engine. A SKIP IS NOT A PASS — the
PostgreSQL class announces loudly when TEST_DATABASE_URL is unset.

DEMONSTRATED FIRST (2026-08-14, pre-cutover head 05a0bba): with only this
test file added, ``PermanentPointerIsNoLongerEnough`` failed on both stores
("'error' not found in {... 'status': 'enrolled' ...}" — the
no-membership player enrolled fine) and ``MembershipGrantsEligibility``
failed the mirrored way (the membership-holding transfer was rejected
``not_eligible``); 23 red in all. Those exact failures are the
permanent-ownership behavior this cutover removes. (The fixtures were then
reshaped for the parity dual-write — the facade now opens stints itself —
so the membershipless player is constructed at the store, as imports do.)
"""

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)
from helpers import FakeClock, end_membership_directly, fresh_sql_store

from hockey_scheduler.api import ApiService
from hockey_scheduler.domain import (
    AuditAction, MembershipStatus, Player, Position, SeasonRosterMembership,
    SubstituteStatus)
from hockey_scheduler.domain.errors import NotEligibleError
from hockey_scheduler.store import InMemoryStore, SqlStore
from hockey_scheduler.web import server as srv

ADMIN = "setup_admin"
UTC = timezone.utc


def _at(hour):
    return datetime(2026, 9, 1, hour, tzinfo=UTC)


class _Fixture:
    """One Program/Season with one League, three registered Teams (home,
    away, third), a venue/rink, and a REGULAR game home-vs-away — which
    therefore carries the LeagueSeason binding (#283 Slice E) the
    membership resolution starts from."""

    def _build(self, store):
        api = ApiService(store)
        api.roster.clock = FakeClock()
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program(
            "Prog", operator_organization_id=org["id"], actor_id=ADMIN)
        season = api.create_season(program["id"], "Fall 2026", actor_id=ADMIN)
        league = api.create_league(season["id"], "Elite", actor_id=ADMIN)
        club = api.create_club("Club", actor_id=ADMIN)
        teams = {}
        for name in ("Home", "Away", "Third"):
            t = api.create_team(club["id"], None, name, actor_id=ADMIN,
                                league_id=league["id"])
            api.register_team_for_season(season["id"], t["id"],
                                         actor_id=ADMIN,
                                         league_id=league["id"])
            teams[name.lower()] = t
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = api.create_ice_slot(rink["id"], _at(18).isoformat(),
                                   _at(19).isoformat(), "game",
                                   actor_id=ADMIN)
        game = api.create_game(season["id"], None, teams["home"]["id"],
                               teams["away"]["id"], slot["id"],
                               actor_id=ADMIN, league_id=league["id"])
        assert "error" not in game, game
        # The premise of the whole file: a regular game is LeagueSeason-bound.
        assert game["league_season_id"], game
        ls_id = game["league_season_id"]
        api.publish_game(game["id"], actor_id=ADMIN)
        return api, season, league, teams, game, ls_id

    def _player(self, api, team_id, name, jersey=None, position="forward"):
        """Facade create — which now ALSO opens the parity stint (the #205
        dual-write), exactly like production."""
        p = api.create_player(team_id, name, position, jersey_number=jersey,
                              actor_id=ADMIN)
        assert "error" not in p, p
        return p

    def _pointer_only_player(self, api, team_id, name, position=None):
        """A player whose permanent pointer is set but whose seasonal record
        is SILENT — the exact shape today's store-level bulk import writes
        (and any pre-059 row a migration has not yet backfilled). The
        facade cannot produce this any more (create_player opens the
        parity stint), which is the point of modelling it at the store."""
        from hockey_scheduler.domain import Player, Position
        player = Player(id=api.store.next_id("player"), team_id=team_id,
                        name=name,
                        position=position or Position.FORWARD)
        api.store.add_player(player)
        return {"id": player.id, "name": name}

    def _stint_id(self, api, player_id, ls_id):
        """The player's auto-opened parity stint on this LeagueSeason."""
        rows = api.list_season_roster_memberships(
            player_id=player_id)["memberships"]
        (m,) = [r for r in rows if r["league_season_id"] == ls_id]
        return m["id"]

    def _transfer(self, api, player_id, from_ls, to_ls, to_team,
                  jersey=None):
        """Record a mid-season transfer the way the seasonal model means
        it: the old stint becomes TRANSFERRED history, a new ACTIVE stint
        opens on the destination. The permanent pointer is deliberately
        left pointing at the old team — post-cutover it no longer governs
        bound-game eligibility.

        The old stint is ended at the STORE (``end_membership_directly``):
        the #205 review round 2 owner ruling made every terminal
        transition through ``set_season_roster_membership_status``
        unconditionally refused until the governed transfer/release
        workflow ships in its own slice, so a terminal PRECONDITION is
        constructed this way rather than by weakening that gate — the
        same pattern the Slice A tests already use."""
        end_membership_directly(
            api.store, self._stint_id(api, player_id, from_ls),
            "transferred")
        return self._membership(api, player_id, to_ls, to_team,
                                jersey=jersey)

    def _membership(self, api, player_id, ls_id, team_id, status="active",
                    jersey=None):
        m = api.create_season_roster_membership(
            player_id, ls_id, team_id, status=status, jersey_number=jersey,
            actor_id=ADMIN)
        assert "error" not in m, m
        return m


class _CutoverContract(_Fixture):
    """Shared Memory + SQLite contract body."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def _each(self):
        for label, store in self._stores():
            api, season, league, teams, game, ls_id = self._build(store)
            yield label, api, season, league, teams, game, ls_id


class PermanentPointerIsNoLongerEnough(_CutoverContract, unittest.TestCase):
    """THE demonstration: permanent ownership alone no longer grants
    eligibility on a LeagueSeason-bound game (pre-cutover it did)."""

    def test_enroll_without_membership_is_rejected(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                ghost = self._pointer_only_player(api, teams["home"]["id"],
                                                  "Ghost")
                # Permanent pointer says "home"; the Season has no
                # membership for them at all (import-path shape).
                res = api.enroll_substitute(game["id"], ghost["id"])
                self.assertIn("error", res, res)
                self.assertEqual(res["error"]["code"], "not_eligible", label)
                self.assertEqual(
                    api.store.substitutes_for_game(game["id"]), [], label)

    def test_block_reason_and_lists_exclude_the_membershipless(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                ghost = self._pointer_only_player(api, teams["home"]["id"],
                                                  "Ghost")
                self.assertEqual(
                    api.roster.substitute_block_reason(ghost["id"],
                                                       game["id"]),
                    "You are not on a team in this game.", label)
                self.assertEqual(
                    api.roster.list_substitute_opportunities(ghost["id"]),
                    [], label)
                addable = api.get_addable_substitutes(
                    game["id"], teams["home"]["id"])["addable"]
                self.assertEqual(addable, [], label)
                # The detail view stays scoped: no membership, no view.
                detail = api.get_substitute_opportunity(ghost["id"],
                                                        game["id"])
                self.assertEqual(detail["error"]["code"], "not_found", label)

    def test_membership_with_third_team_stays_cross_boundary_closed(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                # The facade create itself opened the ACTIVE stint on the
                # third team (parity dual-write) — exactly the cross-boundary
                # case.
                outsider = self._player(api, teams["third"]["id"], "Outsider")
                res = api.enroll_substitute(game["id"], outsider["id"])
                self.assertEqual(res["error"]["code"], "not_eligible", label)

    def test_non_participating_statuses_grant_nothing(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            for status in ("applicant", "inactive", "injured"):
                with self.subTest(backend=label, status=status):
                    p = self._player(api, teams["home"]["id"],
                                     f"P {status}")
                    api.set_season_roster_membership_status(
                        self._stint_id(api, p["id"], ls_id), status,
                        actor_id=ADMIN)
                    res = api.enroll_substitute(game["id"], p["id"])
                    self.assertEqual(res["error"]["code"], "not_eligible",
                                     label)

    def test_released_membership_is_history_not_eligibility(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._player(api, teams["home"]["id"], "Released")
                # Store-level: terminal transitions through the facade are
                # unconditionally refused (#205 round 2 owner ruling) until
                # the governed release workflow ships — see _transfer.
                end_membership_directly(
                    api.store, self._stint_id(api, p["id"], ls_id),
                    "released")
                res = api.enroll_substitute(game["id"], p["id"])
                self.assertEqual(res["error"]["code"], "not_eligible", label)


class MembershipGrantsEligibility(_CutoverContract, unittest.TestCase):
    """The mirrored direction: membership grants what the permanent pointer
    no longer can — including for a player whose pointer is elsewhere."""

    def test_active_membership_with_foreign_pointer_full_flow(self):
        """A mid-season transfer recorded as membership: permanent pointer
        still names the THIRD team, ACTIVE membership names the home team.
        The full enroll -> offer -> accept arc runs, lands the roster entry,
        and audits/notifies exactly as the state machine always has."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._player(api, teams["third"]["id"], "Mover",
                                     jersey=71)
                self._transfer(api, mover["id"], ls_id, ls_id,
                               teams["home"]["id"], jersey=71)
                sub = api.enroll_substitute(game["id"], mover["id"])
                self.assertNotIn("error", sub, (label, sub))
                self.assertEqual(sub["status"], "enrolled", label)
                offered = api.offer_substitute(game["id"], mover["id"])
                self.assertEqual(offered["status"], "offered", label)
                entry = api.accept_substitute(game["id"], mover["id"])
                self.assertNotIn("error", entry, (label, entry))
                actions = [a.action for a in
                           api.store.audit_for_game(game["id"])]
                for expected in (AuditAction.SUBSTITUTE_ENROLLED,
                                 AuditAction.SUBSTITUTE_OFFERED,
                                 AuditAction.SUBSTITUTE_ACCEPTED):
                    self.assertIn(expected, actions, label)
                # The accepted-notification goes to the coach of the team
                # the player RESOLVES to for this game — the home side.
                kinds = [(n.kind.value, n.audience_ref) for n in
                         api.store.all_notifications_feed()]
                self.assertIn(("substitute_accepted", teams["home"]["id"]),
                              kinds, label)

    def test_affiliate_membership_is_the_governed_callup(self):
        """The real call-up shape: the athlete's AUTHORITATIVE active stint
        is on a team of ANOTHER League in the same Season (one open
        membership per LeagueSeason — #205's rule — so the affiliate row
        necessarily lives on a different LeagueSeason), and an AFFILIATE
        membership with the game's team grants eligibility for its games."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                rec = api.create_league(season["id"], "Rec", actor_id=ADMIN)
                club2 = api.create_club("Club 2", actor_id=ADMIN)
                farm = api.create_team(club2["id"], None, "Farm",
                                       actor_id=ADMIN, league_id=rec["id"])
                api.register_team_for_season(season["id"], farm["id"],
                                             actor_id=ADMIN,
                                             league_id=rec["id"])
                rec_ls = api.store.league_season_for(rec["id"], season["id"])
                # The facade create already opened the authoritative ACTIVE
                # stint on the Rec team (parity dual-write, rec_ls).
                callup = self._player(api, farm["id"], "Callup")
                self.assertIsNotNone(
                    self._stint_id(api, callup["id"], rec_ls.id), label)
                self._membership(api, callup["id"], ls_id,
                                 teams["home"]["id"], status="affiliate")
                sub = api.enroll_substitute(game["id"], callup["id"])
                self.assertNotIn("error", sub, (label, sub))
                q = api.get_substitute_candidates(
                    game["id"], teams["home"]["id"])["candidates"]
                self.assertEqual([c["player_id"] for c in q],
                                 [callup["id"]], label)

    def test_player_side_surfaces_resolve_through_membership(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._player(api, teams["third"]["id"], "Mover")
                self._transfer(api, mover["id"], ls_id, ls_id,
                               teams["home"]["id"])
                opps = api.roster.list_substitute_opportunities(mover["id"])
                self.assertEqual([g.id for g in opps], [game["id"]], label)
                detail = api.get_substitute_opportunity(mover["id"],
                                                        game["id"])
                self.assertNotIn("error", detail, (label, detail))
                self.assertTrue(detail["can_accept"], label)
                # The surfaced team is the RESOLVED side, not the pointer's.
                self.assertEqual(detail["team_name"], "Home", label)
                api.enroll_substitute(game["id"], mover["id"])
                api.offer_substitute(game["id"], mover["id"])
                offers = api.roster.list_player_offers(mover["id"])
                self.assertEqual([g.id for g in offers], [game["id"]], label)

    def test_addable_pool_is_the_membership_roster(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                member = self._player(api, teams["home"]["id"], "Member")
                ghost = self._pointer_only_player(api, teams["home"]["id"],
                                                  "Ghost")
                addable = api.get_addable_substitutes(
                    game["id"], teams["home"]["id"])["addable"]
                self.assertEqual([r["player_id"] for r in addable],
                                 [member["id"]], label)
                added = api.add_substitute_candidate(game["id"],
                                                     member["id"],
                                                     actor_id=ADMIN)
                self.assertNotIn("error", added, (label, added))
                self.assertEqual(
                    api.get_substitute_candidates(
                        game["id"], teams["home"]["id"]
                    )["candidates"][0]["player_id"], member["id"], label)
                self.assertEqual(
                    api.add_substitute_candidate(
                        game["id"], ghost["id"],
                        actor_id=ADMIN)["error"]["code"],
                    "not_eligible", label)


class EligibilityIsLiveNotFrozen(_CutoverContract, unittest.TestCase):
    """A membership ended AFTER enrollment fails closed at the next
    transition — same posture as the existing deactivation gate (#270)."""

    def test_release_after_enroll_blocks_offer_and_drops_from_queue(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._player(api, teams["home"]["id"], "Fleeting")
                api.enroll_substitute(game["id"], p["id"])
                # Store-level: terminal transitions through the facade are
                # unconditionally refused (#205 round 2 owner ruling) until
                # the governed release workflow ships — see _transfer.
                end_membership_directly(
                    api.store, self._stint_id(api, p["id"], ls_id),
                    "released")
                # Gone from the live outreach queue (enrollment row remains
                # as history) and no longer offer-able.
                q = api.get_substitute_candidates(
                    game["id"], teams["home"]["id"])["candidates"]
                self.assertEqual(q, [], label)
                res = api.offer_substitute(game["id"], p["id"])
                self.assertEqual(res["error"]["code"], "not_eligible", label)
                row = api.store.substitute_for_player(game["id"], p["id"])
                self.assertEqual(row.status, SubstituteStatus.ENROLLED, label)

    def test_decline_after_release_succeeds_and_notifies_the_real_team(self):
        """#205 blocker 3: unlike offer (a gate, fails closed above),
        decline is a transition on an ALREADY-offered substitute — the
        player must still be able to walk away from a dead offer even
        though their membership ended after it was made. Before the fix,
        ``decline_substitute`` re-derived the coach notification's audience
        from ``team_for_game`` fresh at decline time, got ``None`` (the
        membership resolving it no longer exists), fed that into a
        COACH-audience push, and delivery's #60 fail-closed invariant
        raised — rolling the whole ``@_transactional`` decline back (the
        substitute row stayed ``OFFERED`` forever). Demonstrated fresh,
        tri-store, on the pre-fix code (see PR description): identical
        ``validation_error``/``A coach notification needs an audience_ref``
        on Memory, SQLite AND PostgreSQL (``MembershipCutoverPostgresTest``
        below carries the PostgreSQL half).

        The fix snapshots the team ``offer_substitute`` already resolved
        onto ``SubstituteEnrollment.team_id`` (migration 060) — durable by
        construction, so a membership ending afterward can't undo it — and
        ``decline_substitute`` reads that instead of re-resolving."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._player(api, teams["home"]["id"], "Offered")
                self.assertNotIn(
                    "error", api.enroll_substitute(game["id"], p["id"]),
                    label)
                offer = api.offer_substitute(game["id"], p["id"])
                self.assertNotIn("error", offer, (label, offer))
                end_membership_directly(
                    api.store, self._stint_id(api, p["id"], ls_id),
                    "released")

                before = [(n.kind.value, n.audience_ref) for n in
                         api.store.all_notifications_feed()]
                res = api.decline_substitute(game["id"], p["id"])
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["status"], "declined", label)
                after = [(n.kind.value, n.audience_ref) for n in
                        api.store.all_notifications_feed()]
                new_events = [e for e in after if e not in before]
                # The notification names HOME — the team the now-dead
                # membership used to resolve to — not a crash, and not a
                # silently-dropped notification.
                self.assertIn(
                    ("substitute_declined", teams["home"]["id"]),
                    new_events, (label, new_events))

                row = api.store.substitute_for_player(game["id"], p["id"])
                self.assertEqual(row.status, SubstituteStatus.DECLINED, label)
                self.assertEqual(row.team_id, teams["home"]["id"], label)

    def test_ordinary_decline_with_live_membership_is_unaffected(self):
        """The common case — membership still ACTIVE at decline time — is
        byte-for-byte unchanged: the snapshot taken at offer time and a
        fresh ``team_for_game`` resolution agree, so this passing before
        the fix is not evidence the fix does nothing; it is the fix's own
        regression guard against 'only helps the lapsed case, breaks the
        live one'."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._player(api, teams["home"]["id"], "StillHere")
                api.enroll_substitute(game["id"], p["id"])
                api.offer_substitute(game["id"], p["id"])
                res = api.decline_substitute(game["id"], p["id"])
                self.assertNotIn("error", res, (label, res))
                new_events = [
                    (n.kind.value, n.audience_ref)
                    for n in api.store.all_notifications_feed()]
                self.assertIn(
                    ("substitute_declined", teams["home"]["id"]),
                    new_events, (label, new_events))


class UnboundGamesKeepThePermanentGate(_Fixture, unittest.TestCase):
    """No LeagueSeason binding -> no Season to resolve against: the
    permanent pointer stays the only source, exactly today's behavior.
    (This is also why the pre-existing substitute suites pass unchanged:
    every game they build directly carries no league_season_id.)"""

    def _exhibition(self, api, season, league, teams):
        # Second slot on the fixture's own season-accessible rink.
        rink_id = api.store.get_ice_slot(
            api.store.get_game(  # the regular game's slot -> its rink
                api.store.all_games()[0].id).ice_slot_id).rink_id
        slot = api.create_ice_slot(
            rink_id, _at(20).isoformat(), _at(21).isoformat(), "game",
            actor_id=ADMIN)
        game = api.create_game(season["id"], None, teams["home"]["id"],
                               teams["away"]["id"], slot["id"],
                               actor_id=ADMIN, league_id=league["id"],
                               game_type="exhibition")
        assert "error" not in game, game
        assert game["league_season_id"] is None, game
        api.publish_game(game["id"], actor_id=ADMIN)
        return game

    def test_exhibition_uses_pointer_and_stays_cross_team_closed(self):
        for store in (InMemoryStore(), SqlStore(":memory:")):
            label = type(store).__name__
            api, season, league, teams, _, ls_id = self._build(store)
            with self.subTest(backend=label):
                exhibition = self._exhibition(api, season, league, teams)
                # Pointer-on-home enrolls — even with a SILENT seasonal
                # record (pointer-only row): an unbound game has no Season
                # to resolve against.
                local = self._pointer_only_player(
                    api, teams["home"]["id"], "Local")
                res = api.enroll_substitute(exhibition["id"], local["id"])
                self.assertNotIn("error", res, (label, res))
                # ...pointer-elsewhere stays out, membership or not: this
                # player was membership-transferred to home, but their
                # pointer still says third — and the exhibition judges by
                # pointer alone.
                outsider = self._player(api, teams["third"]["id"], "Out")
                self._transfer(api, outsider["id"], ls_id, ls_id,
                               teams["home"]["id"])
                res = api.enroll_substitute(exhibition["id"], outsider["id"])
                self.assertEqual(res["error"]["code"], "not_eligible", label)

    def test_dangling_league_season_fails_closed(self):
        api, season, league, teams, game, ls_id = self._build(InMemoryStore())
        p = self._player(api, teams["home"]["id"], "P")  # parity stint opens
        # Corrupt the binding at the store level: the LeagueSeason row is
        # gone but the game still claims one. Resolution must reject
        # everyone rather than quietly reverting to permanent ownership.
        del api.store.league_seasons[ls_id]
        res = api.enroll_substitute(game["id"], p["id"])
        self.assertEqual(res["error"]["code"], "not_eligible")


class ParityDualWriteOpensStints(_CutoverContract, unittest.TestCase):
    """The two service edges that used to grant eligibility implicitly now
    record it explicitly — migration 059's rule applied at create time."""

    def test_add_player_after_registration_opens_the_stint(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._player(api, teams["home"]["id"], "Joiner")
                rows = api.list_season_roster_memberships(
                    player_id=p["id"])["memberships"]
                self.assertEqual(
                    [(m["league_season_id"], m["team_id"], m["status"])
                     for m in rows],
                    [(ls_id, teams["home"]["id"], "active")], label)
                # And the stint carries real provenance: a created event
                # with the parity reason, not a silent row.
                events = api.list_season_roster_membership_events(
                    rows[0]["id"])["events"]
                self.assertEqual([e["action"] for e in events],
                                 ["created"], label)
                self.assertIn("permanent-model parity",
                              events[0]["reason"], label)
                self.assertNotIn("error",
                                 api.enroll_substitute(game["id"], p["id"]),
                                 label)

    def test_registering_a_team_opens_stints_for_existing_players(self):
        """The demo's own second ordering: squads added first, the team
        registered afterwards."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                club2 = api.create_club("Late Club", actor_id=ADMIN)
                late = api.create_team(club2["id"], None, "Late",
                                       actor_id=ADMIN,
                                       league_id=league["id"])
                p1 = self._player(api, late["id"], "Early Bird")
                bench = api.create_player(late["id"], "Benched", "forward",
                                          is_active=False, actor_id=ADMIN)
                self.assertEqual(
                    api.list_season_roster_memberships(
                        player_id=p1["id"])["memberships"], [], label)
                api.register_team_for_season(season["id"], late["id"],
                                             actor_id=ADMIN,
                                             league_id=league["id"])
                rows = api.list_season_roster_memberships(
                    player_id=p1["id"])["memberships"]
                self.assertEqual(
                    [(m["team_id"], m["status"]) for m in rows],
                    [(late["id"], "active")], label)
                # A parked player holds no current participation (059 rule).
                self.assertEqual(
                    api.list_season_roster_memberships(
                        player_id=bench["id"])["memberships"], [], label)

    def test_recorded_seasonal_state_wins_over_the_mirror(self):
        """A transfer already recorded in the Season is never overwritten
        by the parity write: re-registering the player's pointer-team
        leaves the transferred player exactly as recorded."""
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                mover = self._player(api, teams["third"]["id"], "Mover")
                self._transfer(api, mover["id"], ls_id, ls_id,
                               teams["home"]["id"])
                reg = api.store.registration_for_team_in_league_season(
                    ls_id, teams["third"]["id"])
                api.unregister_team_from_season(reg.id, actor_id=ADMIN)
                api.register_team_for_season(season["id"],
                                             teams["third"]["id"],
                                             actor_id=ADMIN,
                                             league_id=league["id"])
                rows = api.list_season_roster_memberships(
                    player_id=mover["id"])["memberships"]
                self.assertEqual(
                    sorted((m["team_id"], m["status"]) for m in rows),
                    sorted([(teams["third"]["id"], "transferred"),
                            (teams["home"]["id"], "active")]), label)


class LeagueSeasonExactnessFixture:
    """Two Leagues ('Elite', 'House') sharing ONE Season — the ORDINARY
    shape, not an edge case: ``LeagueSeason``'s own uniqueness is
    ``(league_id, season_id)``, one row per League *per* Season, so any
    Season running more than one League produces sibling ``LeagueSeason``
    rows that share a ``season_id``. This is the exact shape the #205
    review's resolver-design blocker identified: deriving a game's
    ``season_id`` and re-matching memberships on THAT column alone accepts
    a membership scoped to a SIBLING LeagueSeason of the same Season. House
    fields the two sides of the REGULAR game under test; Elite exists only
    to hold the conflated membership."""

    def _build_two_league(self, store):
        api = ApiService(store)
        api.roster.clock = FakeClock()
        org = api.create_organization("Org", "O", actor_id=ADMIN)
        program = api.create_program(
            "Prog", operator_organization_id=org["id"], actor_id=ADMIN)
        season = api.create_season(program["id"], "Fall 2026", actor_id=ADMIN)
        elite = api.create_league(season["id"], "Elite", actor_id=ADMIN)
        house = api.create_league(season["id"], "House", actor_id=ADMIN)
        club = api.create_club("Club", actor_id=ADMIN)
        elite_team = api.create_team(club["id"], None, "Elite Team",
                                     actor_id=ADMIN, league_id=elite["id"])
        api.register_team_for_season(season["id"], elite_team["id"],
                                     actor_id=ADMIN, league_id=elite["id"])
        house_home = api.create_team(club["id"], None, "House Home",
                                     actor_id=ADMIN, league_id=house["id"])
        api.register_team_for_season(season["id"], house_home["id"],
                                     actor_id=ADMIN, league_id=house["id"])
        house_away = api.create_team(club["id"], None, "House Away",
                                     actor_id=ADMIN, league_id=house["id"])
        api.register_team_for_season(season["id"], house_away["id"],
                                     actor_id=ADMIN, league_id=house["id"])
        venue = api.create_venue("V", organization_id=org["id"],
                                 league_id=program["id"], actor_id=ADMIN)
        api.grant_season_venue_access(season["id"], venue["id"],
                                      actor_id=ADMIN)
        rink = api.create_rink(venue["id"], "R", actor_id=ADMIN)
        slot = api.create_ice_slot(rink["id"], _at(18).isoformat(),
                                   _at(19).isoformat(), "game",
                                   actor_id=ADMIN)
        house_game = api.create_game(
            season["id"], None, house_home["id"], house_away["id"],
            slot["id"], actor_id=ADMIN, league_id=house["id"])
        assert "error" not in house_game, house_game
        house_ls_id = house_game["league_season_id"]
        assert house_ls_id, house_game
        api.publish_game(house_game["id"], actor_id=ADMIN)
        elite_ls = api.store.league_season_for(elite["id"], season["id"])
        house_ls = api.store.get_league_season(house_ls_id)
        # The fixture's own premise: one shared Season, two DIFFERENT
        # LeagueSeason rows.
        assert elite_ls.season_id == house_ls.season_id, (elite_ls, house_ls)
        assert elite_ls.id != house_ls.id, (elite_ls, house_ls)
        return {
            "api": api, "season": season, "elite": elite, "house": house,
            "elite_team": elite_team, "house_home": house_home,
            "house_away": house_away, "house_game": house_game,
            "elite_ls": elite_ls, "house_ls": house_ls,
        }

    def _conflated_player(self, api, elite_ls, wrong_team_id, name):
        """A membership scoped to ``elite_ls`` but naming a House team — the
        shape ``create_season_roster_membership`` itself would refuse
        (``team.league_id == ls.league_id`` — setup_service.py rule 7
        analog), constructed directly at the STORE the same way this file's
        own ``_pointer_only_player`` models an unguarded future writer
        (store-level bulk import). Uses a store-level ``Player`` too, so no
        parity dual-write opens a competing (and colliding) stint on
        ``elite_ls`` for this player."""
        player = Player(id=api.store.next_id("player"),
                        team_id=wrong_team_id, name=name,
                        position=Position.FORWARD)
        api.store.add_player(player)
        m = SeasonRosterMembership(
            id=api.store.next_id("srm"), player_id=player.id,
            league_season_id=elite_ls.id, season_id=elite_ls.season_id,
            team_id=wrong_team_id, status=MembershipStatus.ACTIVE)
        api.store.add_season_roster_membership(m)
        return {"id": player.id, "name": name}


class LeagueSeasonExactness(LeagueSeasonExactnessFixture, unittest.TestCase):
    """#205 review resolver-design blocker: eligibility must match a game's
    EXACT ``league_season_id``, never a Season derived from it — proven both
    directions on Memory + SQLite."""

    def _stores(self):
        yield "memory", InMemoryStore()
        yield "sqlite", SqlStore(":memory:")

    def test_sibling_league_season_membership_is_not_eligible(self):
        for label, store in self._stores():
            with self.subTest(backend=label):
                f = self._build_two_league(store)
                api, house_game = f["api"], f["house_game"]
                wrong = self._conflated_player(
                    api, f["elite_ls"], f["house_home"]["id"], "WrongLS")
                game_obj = api.store.get_game(house_game["id"])
                player_obj = api.store.get_player(wrong["id"])
                # THE defect this resolver fixes: a season_id-derived match
                # would accept this membership (elite_ls and house_ls share
                # a season_id); the exact league_season_id match must not.
                self.assertIsNone(
                    api.roster.team_for_game(game_obj, player_obj), label)
                self.assertIsNone(
                    api.roster.resolve_membership(game_obj, player_obj),
                    label)
                res = api.enroll_substitute(house_game["id"], wrong["id"])
                self.assertEqual(res["error"]["code"], "not_eligible", label)

    def test_membership_on_the_games_own_league_season_is_eligible(self):
        """The mirrored direction, proving the fix isn't simply "reject
        everyone": a membership scoped to the game's OWN LeagueSeason, on
        the SAME team a conflated row would have named, resolves normally."""
        for label, store in self._stores():
            with self.subTest(backend=label):
                f = self._build_two_league(store)
                api, house_game, house_home = (
                    f["api"], f["house_game"], f["house_home"])
                right = api.create_player(house_home["id"], "RightLS",
                                          "forward", actor_id=ADMIN)
                assert "error" not in right, right
                game_obj = api.store.get_game(house_game["id"])
                player_obj = api.store.get_player(right["id"])
                self.assertEqual(
                    api.roster.team_for_game(game_obj, player_obj),
                    house_home["id"], label)
                res = api.enroll_substitute(house_game["id"], right["id"])
                self.assertNotIn("error", res, (label, res))
                self.assertEqual(res["status"], "enrolled", label)


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "PostgreSQL not configured (TEST_DATABASE_URL) — the #205 review "
    "resolver-design blocker (LeagueSeason exactness) was NOT exercised on "
    "PostgreSQL. A SKIP HERE IS NOT A PASS.")
class LeagueSeasonExactnessPostgresTest(LeagueSeasonExactnessFixture,
                                        unittest.TestCase):
    """Both directions of the LeagueSeason-exactness fix against real
    PostgreSQL."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)

    def test_sibling_league_season_conflation_on_postgres(self):
        f = self._build_two_league(self.store)
        api, house_game = f["api"], f["house_game"]
        wrong = self._conflated_player(
            api, f["elite_ls"], f["house_home"]["id"], "WrongLS")
        game_obj = api.store.get_game(house_game["id"])
        player_obj = api.store.get_player(wrong["id"])
        self.assertIsNone(api.roster.team_for_game(game_obj, player_obj))
        self.assertEqual(
            api.enroll_substitute(house_game["id"], wrong["id"]
                                  )["error"]["code"],
            "not_eligible")
        right = api.create_player(f["house_home"]["id"], "RightLS",
                                  "forward", actor_id=ADMIN)
        self.assertEqual(
            api.roster.team_for_game(game_obj, api.store.get_player(right["id"])),
            f["house_home"]["id"])
        self.assertEqual(
            api.enroll_substitute(house_game["id"], right["id"])["status"],
            "enrolled")


class SeasonScopedPositionResolvesCorrectly(_CutoverContract,
                                            unittest.TestCase):
    """#205 review resolver-design blocker: every substitute-facing read of
    a player's position for a LeagueSeason-bound game must use the
    SEASON-SCOPED ``SeasonRosterMembership.position`` (this stint), never
    the permanent ``Player.position`` — proven with a player whose
    permanent position (FORWARD) differs from their corrected season-scoped
    one (GOALIE)."""

    def _goalie_by_membership(self, api, team_id, ls_id, name):
        """A player whose PERMANENT position stays FORWARD but whose
        season-scoped stint is corrected to GOALIE via the governed update
        path — the shape a team recording "plays goalie this season"
        without touching the player's permanent record produces."""
        p = self._player(api, team_id, name, position="forward")
        stint_id = self._stint_id(api, p["id"], ls_id)
        upd = api.update_season_roster_membership(
            stint_id, position="goalie", actor_id=ADMIN)
        assert "error" not in upd, upd
        assert upd["position"] == "goalie", upd
        return p

    def test_block_reason_and_enrollment_use_the_membership_position(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._goalie_by_membership(
                    api, teams["home"]["id"], ls_id, "Two-Way")
                # The player's PERMANENT record still says forward.
                self.assertEqual(
                    api.store.get_player(p["id"]).position.value,
                    "forward", label)
                # substitute_block_reason's needed-slot check must use the
                # season-scoped GOALIE, not the permanent forward — a
                # skater-only read would find no reason to block a skater
                # slot demand that doesn't exist for this player.
                reason = api.roster.substitute_block_reason(
                    p["id"], game["id"])
                self.assertIsNone(reason, (label, reason))
                sub = api.enroll_substitute(game["id"], p["id"])
                self.assertNotIn("error", sub, (label, sub))
                self.assertEqual(sub["position"], "goalie", label)
                # The outreach queue's position/slot_type pair agrees.
                cand = api.get_substitute_candidates(
                    game["id"], teams["home"]["id"])["candidates"]
                row = [c for c in cand if c["player_id"] == p["id"]][0]
                self.assertEqual(row["position"], "goalie", label)
                self.assertEqual(row["slot_type"], "goalie", label)

    def test_addable_pool_reflects_the_membership_position(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._goalie_by_membership(
                    api, teams["home"]["id"], ls_id, "Bench Goalie")
                addable = api.get_addable_substitutes(
                    game["id"], teams["home"]["id"])["addable"]
                row = [r for r in addable if r["player_id"] == p["id"]][0]
                self.assertEqual(row["position"], "goalie", label)
                self.assertEqual(row["slot_type"], "goalie", label)

    def test_http_opportunity_detail_reflects_the_membership_position(self):
        for label, api, season, league, teams, game, ls_id in self._each():
            with self.subTest(backend=label):
                p = self._goalie_by_membership(
                    api, teams["home"]["id"], ls_id, "Home Goalie")
                detail = api.get_substitute_opportunity(p["id"], game["id"])
                self.assertNotIn("error", detail, (label, detail))
                self.assertEqual(
                    detail["position_needed"], "goalie", label)


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "PostgreSQL not configured (TEST_DATABASE_URL) — the #205 review "
    "resolver-design blocker (season-scoped position) was NOT exercised on "
    "PostgreSQL. A SKIP HERE IS NOT A PASS.")
class SeasonScopedPositionPostgresTest(_Fixture, unittest.TestCase):
    """The season-scoped position fix against real PostgreSQL."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = self._build(self.store)

    def test_membership_position_overrides_permanent_on_postgres(self):
        api, teams, game, ls_id = (
            self.api, self.teams, self.game, self.ls_id)
        p = self._player(api, teams["home"]["id"], "Two-Way",
                         position="forward")
        stint_id = self._stint_id(api, p["id"], ls_id)
        upd = api.update_season_roster_membership(
            stint_id, position="goalie", actor_id=ADMIN)
        self.assertNotIn("error", upd, upd)
        sub = api.enroll_substitute(game["id"], p["id"])
        self.assertNotIn("error", sub, sub)
        self.assertEqual(sub["position"], "goalie")
        detail = api.get_substitute_opportunity(p["id"], game["id"])
        # Already enrolled — detail still reports the position it resolves
        # for the game, independent of the ENROLLED-vs-not branch.
        self.assertNotIn("error", detail, detail)


class ResolverFixOverHttp(unittest.TestCase):
    """Both #205 review resolver-design blocker fixes, exercised through a
    REAL HTTP request against ``web/server.py`` — not just the facade in
    isolation. Builds its own LeagueSeason-bound fixture on the live demo
    server state (``srv.STATE``) via the setup facade, so the games under
    test carry a genuine ``league_season_id`` (the existing HTTP substitute
    fixtures in ``test_substitute_opportunity.py`` build games directly at
    the store with no League binding at all, which never reaches the
    membership-resolution branch under test here)."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        f = LeagueSeasonExactnessFixture()
        self.fx = f._build_two_league(self.api.store)
        self.goalie = self._goalie_by_membership(self.fx, "HTTP Goalie")
        self.wrong_ls = f._conflated_player(
            self.api, self.fx["elite_ls"], self.fx["house_home"]["id"],
            "HTTP WrongLS")
        for pid, username in ((self.goalie["id"], "httpgoalie"),
                              (self.wrong_ls["id"], "httpwrongls")):
            self.api.accounts.create_account(
                username, "demo", "player",
                scope={"team_id": self.fx["house_home"]["id"],
                      "player_id": pid},
                actor_id="test_seed")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _goalie_by_membership(self, fx, name):
        api = self.api
        p = api.create_player(fx["house_home"]["id"], name, "forward",
                              actor_id=ADMIN)
        assert "error" not in p, p
        rows = api.list_season_roster_memberships(
            player_id=p["id"])["memberships"]
        (stint,) = [r for r in rows if r["league_season_id"] == fx["house_ls"].id]
        upd = api.update_season_roster_membership(
            stint["id"], position="goalie", actor_id=ADMIN)
        assert "error" not in upd, upd
        return p

    def _login(self, username):
        c = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/auth/login",
            data=json.dumps({"username": username,
                            "password": "demo"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with c.open(req) as r:
            self.assertEqual(r.status, 200)
        return c

    def _get(self, opener, path):
        try:
            with opener.open(
                    f"http://127.0.0.1:{self.port}{path}") as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_position_fix_over_http(self):
        """The signed-in player's own opportunity detail reports the
        SEASON-SCOPED position (goalie), not the permanent one (forward)."""
        c = self._login("httpgoalie")
        gid = self.fx["house_game"]["id"]
        status, d = self._get(c, f"/api/me/substitute-opportunities/{gid}")
        self.assertEqual(status, 200, d)
        self.assertEqual(d["position_needed"], "goalie")

    def test_leagueseason_exactness_fix_over_http(self):
        """A player whose ONLY membership sits on a SIBLING LeagueSeason of
        the game's own Season gets the SAME 404 a genuinely unrelated
        player would — the exact-match fix, not just a facade-level
        assertion."""
        c = self._login("httpwrongls")
        gid = self.fx["house_game"]["id"]
        status, d = self._get(c, f"/api/me/substitute-opportunities/{gid}")
        self.assertEqual(status, 404, d)


_PG_SKIP = ("PostgreSQL not configured (TEST_DATABASE_URL) or psycopg "
            "missing — the #205 substitute eligibility cutover was NOT "
            "exercised on PostgreSQL. A SKIP HERE IS NOT A PASS: the "
            "membership reads behind the gate are real SQL here (row locks, "
            "enum/int round-trips) and the enroll->offer->accept arc must "
            "hold inside PostgreSQL transactions. Set TEST_DATABASE_URL "
            "(run_parallel.py --postgres does) to run it.")


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), _PG_SKIP)
class MembershipCutoverPostgresTest(_Fixture, unittest.TestCase):
    """Both cutover directions plus the full arc against real PostgreSQL."""

    def setUp(self):
        self.store = fresh_sql_store(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.store.close)
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = self._build(self.store)

    def test_cutover_both_directions_on_postgres(self):
        api, game, teams = self.api, self.game, self.teams
        ghost = self._pointer_only_player(api, teams["home"]["id"], "Ghost")
        self.assertEqual(
            api.enroll_substitute(game["id"], ghost["id"])["error"]["code"],
            "not_eligible")
        mover = self._player(api, teams["third"]["id"], "Mover", jersey=71)
        self._transfer(api, mover["id"], self.ls_id, self.ls_id,
                       teams["home"]["id"], jersey=71)
        self.assertEqual(
            api.enroll_substitute(game["id"], mover["id"])["status"],
            "enrolled")
        api.offer_substitute(game["id"], mover["id"])
        entry = api.accept_substitute(game["id"], mover["id"])
        self.assertNotIn("error", entry, entry)
        row = self.store.substitute_for_player(game["id"], mover["id"])
        self.assertEqual(row.status, SubstituteStatus.ACCEPTED)

    def test_decline_after_release_succeeds_on_postgres(self):
        """#205 blocker 3 against real PostgreSQL — see
        ``EligibilityIsLiveNotFrozen.test_decline_after_release_succeeds_
        and_notifies_the_real_team`` for the Memory/SQLite half and the
        full defect narrative. Demonstrated pre-fix on this exact class'
        fixture: ``validation_error`` / ``A coach notification needs an
        audience_ref``, substitute row stuck ``OFFERED``."""
        api, game, teams, ls_id = self.api, self.game, self.teams, self.ls_id
        p = self._player(api, teams["home"]["id"], "PG Offered")
        self.assertNotIn(
            "error", api.enroll_substitute(game["id"], p["id"]))
        offer = api.offer_substitute(game["id"], p["id"])
        self.assertNotIn("error", offer, offer)
        end_membership_directly(
            api.store, self._stint_id(api, p["id"], ls_id), "released")

        res = api.decline_substitute(game["id"], p["id"])
        self.assertNotIn("error", res, res)
        self.assertEqual(res["status"], "declined")

        row = self.store.substitute_for_player(game["id"], p["id"])
        self.assertEqual(row.status, SubstituteStatus.DECLINED)
        self.assertEqual(row.team_id, teams["home"]["id"])

        new_events = [(n.kind.value, n.audience_ref) for n in
                      self.store.all_notifications_feed()]
        self.assertIn(
            ("substitute_declined", teams["home"]["id"]), new_events,
            new_events)


class DeclineAfterMembershipEndOverHttp(unittest.TestCase):
    """The #205 blocker 3 fix exercised through a REAL HTTP request against
    ``web/server.py`` — enroll/offer/decline all go through a real socket
    request, matching the pattern ``ResolverFixOverHttp``/
    ``SlotOverfillOverHttp`` established for the other #205 review
    blockers. The membership-ending step has no HTTP surface worth
    exercising (it is a store-level-only precondition — see
    ``end_membership_directly``'s own docstring for why even the setup
    facade cannot reach a terminal transition), so it is seeded directly
    at the live demo server state (``srv.STATE``), like the other HTTP
    fixtures in this file do for their own setup steps."""

    def setUp(self):
        srv.STATE.reset(seed=False)
        self.api = srv.STATE.api
        fx = _Fixture()
        (self.api, self.season, self.league, self.teams, self.game,
         self.ls_id) = fx._build(self.api.store)
        self.fx = fx
        self.player = fx._player(self.api, self.teams["home"]["id"],
                                 "HTTP Offered")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._teardown_server)

    def _teardown_server(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def _req(self, method, path, body=None, role="league_admin"):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if role is not None:
            req.add_header("X-Demo-Role", role)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_decline_after_release_succeeds_over_http(self):
        gid = self.game["id"]
        pid = self.player["id"]

        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/enroll",
            {"player_id": pid})
        self.assertEqual(status, 200, r)
        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/{pid}/offer", {})
        self.assertEqual(status, 200, r)

        stint_id = self.fx._stint_id(self.api, pid, self.ls_id)
        end_membership_directly(self.api.store, stint_id, "released")

        status, r = self._req(
            "POST", f"/api/games/{gid}/substitutes/{pid}/decline", {})
        self.assertEqual(status, 200, r)
        self.assertEqual(r["status"], "declined", r)

        status, row = self._req(
            "GET", f"/api/games/{gid}/roster-status")
        self.assertEqual(status, 200, row)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
