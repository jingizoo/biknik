"""Canonical League resolution: the NEGATIVE + ZERO-MUTATION matrix (#364).

#364 makes the SERVER own the (Program, Season, League) tuple: when both a
Season and a League are submitted, ``ContextService._canonical_league_season``
either keeps the current Season (it binds), moves atomically to the League's one
valid authorized ACTIVE binding, or refuses. The POSITIVE half of that ruling
(keep / move / never-tie-break) is proven in ``test_active_context_league.py``.

This file owns the other half, which is where a resolver of this shape actually
goes wrong: every way a League selection can be refused must be

  * **generic** — ONE non-oracle reason, ``league_not_accessible``, so the
    context bar can never be used to probe for records the caller may not see;
    the refusals must be mutually INDISTINGUISHABLE, message included;
  * **inert** — the previously saved context row is byte-identical afterwards,
    ``updated_at`` included. A refusal that half-committed (or merely bumped the
    row) would silently relocate an operator, which is the exact class of defect
    the ruling's "never choose a Season on the operator's behalf" rule exists to
    prevent;
  * **non-creating** — resolution READS bindings. It must never conjure the
    ``LeagueSeason`` row that would have made the refused selection legal;
    creating a binding stays the authorized, audited job of
    ``setup_service.create_league_season``.

Each case therefore establishes a known-good prior context FIRST, so "zero
mutation" is provable against a real persisted row read back through the store —
not against absence, which any no-op would satisfy.

Backends: memory / sqlite / postgres via the shared ``_backends()`` pattern;
postgres is SKIPPED unless ``TEST_DATABASE_URL`` is set.
"""

import os
import unittest

from helpers import BACKEND  # noqa: F401  (ensures sys.path is set up)

from hockey_scheduler.api.service import ApiService
from hockey_scheduler.domain import Role, SeasonStatus
from hockey_scheduler.services.context_service import ContextService
from hockey_scheduler.store import InMemoryStore, SqlStore

ADMIN = (Role.LEAGUE_ADMIN, {})
REFUSED = "league_not_accessible"


def _backends():
    yield "memory", InMemoryStore()
    yield "sqlite", SqlStore(":memory:")
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        SqlStore(url).clear_all_data()
        yield "postgres", SqlStore(url)


def _close(store):
    if isinstance(store, SqlStore):
        store.close()


def _program_season_league(api, pname="P1", sname="S1", lname="Gold"):
    """A Program + Season + a League BOUND to that Season. Returns ids."""
    pid = api.create_program(pname, "US", "UTC")["id"]
    sid = api.create_season(pid, sname)["id"]
    lid = api.create_league(sid, lname)["id"]
    return pid, sid, lid


def _registered_team(api, sid, lid, tname="Alpha"):
    """A Club+Team permanently in League ``lid`` and registered in Season ``sid``."""
    did = api.create_division(sid, "D1", league_id=lid)["id"]
    club = api.create_club("Club")["id"]
    team = api.create_team(club_id=club, name=tname, league_id=lid,
                           division_id=did)["id"]
    api.setup.register_team_for_season(sid, team, did)
    return team


def _unbind(store, league_id, season_id):
    """Remove the LeagueSeason binding directly (the store-level effect of an
    authorized unbind), leaving the permanent League itself intact."""
    ls = store.league_season_for(league_id, season_id)
    if ls is not None:
        with store.transaction():
            store.delete_league_season(ls.id)


def _row(store, user_id="u1"):
    """The saved context row read back through the store, as a comparable tuple.

    ``updated_at`` is included deliberately: a refusal must not even TOUCH the
    row, and last-write-wins ordering reads this column. A mutation harness
    confirms it is load-bearing — a refusal path that rewrote byte-identical
    values with a fresh clock is caught ONLY by this field.

    What this catches, precisely: a write that survives the refusal. A write
    made INSIDE the failed transaction is already undone by rollback on every
    backend, so the exposure that remains is a *compensating* write outside it —
    the plausible-looking "clear the League so the UI stops showing a stale one"
    on the error path. That is the mutant this assertion is aimed at."""
    ctx = store.get_active_context(user_id)
    if ctx is None:
        return None
    return (ctx.id, ctx.program_id, ctx.season_id, ctx.league_id, ctx.updated_at)


def _bindings(store):
    """Every LeagueSeason row, by identity — the "never creates a binding" probe."""
    return sorted((ls.id, ls.league_id, ls.season_id)
                  for ls in store.all_league_seasons())


class _RefusalCase(unittest.TestCase):
    """Shared machinery: refuse, then prove nothing moved."""

    def _refusal(self, fn):
        """``(reason, message)`` for a call that MUST raise. Returning the
        message too is the point: the refusals have to be indistinguishable in
        what a caller can actually observe, not merely in a details key."""
        try:
            fn()
        except Exception as exc:   # NotFoundError carries .details
            return getattr(exc, "details", {}).get("reason"), str(exc)
        self.fail("the selection was accepted; it must have been refused")

    def _assert_inert(self, store, before, bindings, label):
        self.assertEqual(
            _row(store), before,
            f"{label}: a refused selection mutated the saved context row")
        self.assertEqual(
            _bindings(store), bindings,
            f"{label}: a refused selection changed the LeagueSeason table")

    def _refuse_and_assert_inert(self, store, fn, label):
        """Refuse ``fn``, assert the generic reason, and assert zero mutation of
        both the saved row and the binding table. Returns ``(reason, message)``."""
        before, bindings = _row(store), _bindings(store)
        self.assertIsNotNone(
            before, f"{label}: the case must establish a prior context first")
        reason, msg = self._refusal(fn)
        self.assertEqual(reason, REFUSED, (label, msg))
        self._assert_inert(store, before, bindings, label)
        return reason, msg


class CanonicalLeagueRefusalTest(_RefusalCase):
    """Every refusal shape, each proving the saved row is byte-identical after."""

    def test_cross_program_league_refuses_inertly(self):
        """A League under another Program is not a candidate at all — the
        Program-scoped authorization gate runs BEFORE any binding is considered,
        which is what keeps this indistinguishable from "does not exist"."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pA, sA, lA = _program_season_league(api, "PA", "SA", "GoldA")
                _pB, _sB, lB = _program_season_league(api, "PB", "SB", "GoldB")
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pA, sA, lA)   # known-good prior

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *ADMIN, pA, sA, lB),
                    label)
                # The prior selection still resolves — the refusal was inert,
                # not merely invisible.
                self.assertEqual(
                    [o.id for o in svc.resolve_with_league("u1", *ADMIN)],
                    [pA, sA, lA], label)
                _close(store)

    def test_unauthorized_league_refuses_inertly_for_a_scoped_role(self):
        """A Coach may select their own Team's League and nothing else. Silver
        exists, is in the same Program, and is bound to the same Season — the
        ONLY thing wrong with it is scope, so this leg fails exactly when the
        League authorization filter is bypassed."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)
                coach = (Role.COACH, {"team_id": team})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, pid, sid, gold)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *coach, pid, sid, silver),
                    label)
                # A global operator CAN select it, so the refusal is about this
                # caller's scope rather than about Silver being broken.
                self.assertEqual(
                    svc.set_with_league("u2", *ADMIN, pid, sid, silver)[2].id,
                    silver, label)
                _close(store)

    def test_deleted_league_refuses_inertly(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, silver)

                _unbind(store, gold, sid)
                with store.transaction():
                    store.delete_league(gold)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *ADMIN, pid, sid, gold),
                    label)
                _close(store)

    def test_league_whose_only_bindings_season_was_deleted_refuses_inertly(self):
        """Rule 2 must resolve a candidate Season it can actually SEE. A binding
        pointing at a deleted Season is a dangling row: it may not be counted as
        the League's "exactly one valid active binding", and the League must not
        be moved into a Season that no longer exists."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, orphan = _program_season_league(api, "P", "S1", "Orphan")
                s2 = api.create_season(pid, "S2")["id"]
                anchor = api.create_league(s2, "Anchor")["id"]
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, s2, anchor)

                with store.transaction():          # the binding row survives
                    store.delete_season(s1)
                self.assertIsNotNone(store.league_season_for(orphan, s1), label)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, orphan),
                    label)
                _close(store)

    def test_unbound_league_refuses_inertly_and_invents_no_binding(self):
        """A League with NO LeagueSeason rows at all. The tempting "fix" for the
        release blocker was to bind on demand; the ruling forbids it, so this
        asserts the binding table is unchanged and the League is still unbound
        after the refusal."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S", "Gold")
                loose = api.create_league(sid, "Loose")["id"]
                _unbind(store, loose, sid)
                self.assertEqual(store.league_seasons_for_league(loose), [], label)
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, sid, gold)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *ADMIN, pid, sid, loose),
                    label)
                self.assertEqual(
                    store.league_seasons_for_league(loose), [],
                    f"{label}: resolution CREATED a LeagueSeason binding")
                _close(store)

    def test_revoked_authorization_between_set_and_reselect_refuses_inertly(self):
        """The Team transfers out of the League AFTER the context was saved, and
        re-selecting the same League is now refused.

        Deliberately a PROGRAM-ONLY context (no Season), exactly as the sibling
        fallback test is: with a Season selected the same transfer also revokes
        Season authorization, so the call would be refused by the Season gate and
        this would pass without League authorization ever being consulted —
        green for the wrong reason."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, sid, gold = _program_season_league(api, "P", "S", "Gold")
                silver = api.create_league(sid, "Silver")["id"]
                team = _registered_team(api, sid, gold)
                coach = (Role.COACH, {"team_id": team})
                svc = ContextService(store)
                svc.set_with_league("u1", *coach, pid, None, gold)
                self.assertEqual(_row(store)[3], gold, label)

                t = store.get_team(team)            # transferred out of Gold
                t.league_id = silver
                store.save_team(t)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *coach, pid, None, gold),
                    label)
                # Gold itself is intact and still bound — only this caller's
                # authorization changed.
                self.assertIsNotNone(store.league_season_for(gold, sid), label)
                _close(store)

    def test_archived_only_binding_refuses_inertly(self):
        """Rule 2 resolves only ACTIVE bindings, so a League bound solely to an
        archived Season refuses — and must do so without relocating the operator
        into that read-only history."""
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, lid = _program_season_league(api, "P", "S1", "Gold")
                s2 = api.create_season(pid, "S2")["id"]
                anchor = api.create_league(s2, "Anchor")["id"]
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, s2, anchor)

                archived = store.get_season(s1)
                archived.status = SeasonStatus.ARCHIVED
                store.save_season(archived)

                self._refuse_and_assert_inert(
                    store,
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, lid),
                    label)
                _close(store)


class CanonicalSeasonProgramMismatchTest(_RefusalCase):
    """A Season from ANOTHER Program is refused by the Season axis, before
    canonical League resolution runs at all.

    This is the one refusal in the matrix that does NOT report
    ``league_not_accessible``, and that is correct rather than a gap: the Season
    axis has its own equally generic non-oracle reason, and reaching the League
    resolver with a cross-Program Season is precisely what must not happen —
    otherwise a caller could use the League axis to smuggle a Season they cannot
    see into their own Program's context. Asserted explicitly so that a future
    refactor which reorders the two gates fails here loudly."""

    def test_season_from_another_program_refuses_inertly(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pA, sA, lA = _program_season_league(api, "PA", "SA", "GoldA")
                _pB, sB, _lB = _program_season_league(api, "PB", "SB", "GoldB")
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pA, sA, lA)
                before, bindings = _row(store), _bindings(store)

                reason, msg = self._refusal(
                    lambda: svc.set_with_league("u1", *ADMIN, pA, sB, lA))
                self.assertEqual(reason, "season_not_accessible", (label, msg))
                self.assertNotIn(sB, msg, f"{label}: the message names the Season")
                self._assert_inert(store, before, bindings, label)
                # The out-of-Program Season was never adopted, and no binding was
                # invented to make the pair legal.
                self.assertIsNone(store.league_season_for(lA, sB), label)
                _close(store)


class CanonicalRefusalsAreIndistinguishableTest(_RefusalCase):
    """Every League-axis refusal is the SAME observable outcome.

    Built in one store so the comparison is between real, simultaneously-valid
    shapes rather than between separately-constructed fixtures. If any branch
    ever grows its own reason or its own message — "no binding in this season",
    "league is archived", "not authorized" — this fails, because each of those
    would confirm the League EXISTS to a caller who may not see it."""

    def _cases(self, api, store, svc):
        pid, s1, gold = _program_season_league(api, "P", "S1", "Gold")
        s2 = api.create_season(pid, "S2")["id"]
        anchor = api.create_league(s2, "Anchor")["id"]      # bound to S2 only
        silver = api.create_league(s1, "Silver")["id"]
        loose = api.create_league(s1, "Loose")["id"]
        _unbind(store, loose, s1)                           # no bindings at all
        archived_only = api.create_league(s1, "Old")["id"]

        gone = api.create_league(s1, "Gone")["id"]
        _unbind(store, gone, s1)
        with store.transaction():
            store.delete_league(gone)

        team = _registered_team(api, s1, gold)
        coach = (Role.COACH, {"team_id": team})

        # S1 archived last, so every fixture above could be built normally: it
        # makes Gold/Silver/Old archived-only, i.e. never auto-enterable.
        archived = store.get_season(s1)
        archived.status = SeasonStatus.ARCHIVED
        store.save_season(archived)

        # The known-good prior context every case is measured against.
        svc.set_with_league("u1", *ADMIN, pid, s2, anchor)

        return {
            # A League that does not exist at all — the oracle baseline every
            # other refusal must be indistinguishable from.
            "nonexistent": lambda: svc.set_with_league(
                "u1", *ADMIN, pid, s2, "league_does_not_exist"),
            "deleted": lambda: svc.set_with_league("u1", *ADMIN, pid, s2, gone),
            "unbound": lambda: svc.set_with_league("u1", *ADMIN, pid, s2, loose),
            "archived_only": lambda: svc.set_with_league(
                "u1", *ADMIN, pid, s2, archived_only),
            # The Coach's OWN authorized Season (S1) with a League that is not
            # theirs: scope is the only thing wrong with it, so the refusal
            # cannot be coming from the Season gate.
            "unauthorized": lambda: svc.set_with_league(
                "u1", *coach, pid, s1, silver),
            "cross_program": lambda: svc.set_with_league(
                "u1", *ADMIN, pid, s2, self._foreign_league),
        }

    def test_all_league_refusals_report_one_identical_outcome(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                # A whole other Program, for the cross-Program case.
                self._foreign_league = _program_season_league(
                    api, "PB", "SB", "Foreign")[2]
                svc = ContextService(store)
                cases = self._cases(api, store, svc)

                before, bindings = _row(store), _bindings(store)
                observed = {}
                for name, call in cases.items():
                    observed[name] = self._refusal(call)
                    # Inert after EVERY case, not merely after the last one.
                    self._assert_inert(store, before, bindings, f"{label}/{name}")

                self.assertEqual(
                    len(set(observed.values())), 1,
                    f"{label}: refusals are distinguishable: {observed}")
                reason, message = next(iter(observed.values()))
                self.assertEqual(reason, REFUSED, (label, observed))
                # And the message leaks nothing either.
                for token in ("archiv", "bound", "authoriz", "delet", "program"):
                    self.assertNotIn(token, message.lower(), (label, message))

                # Positive control: the store is not simply wedged — a genuinely
                # valid selection still commits, and moves the row.
                _p, season, league = svc.set_with_league(
                    "u1", *ADMIN, before[1], before[2], before[3])
                self.assertEqual((season.id, league.id), (before[2], before[3]),
                                 label)
                _close(store)


class CanonicalRefusalCreatesNoBindingTest(_RefusalCase):
    """The single strongest invariant, asserted globally: not one refusal in the
    whole matrix adds a ``LeagueSeason`` row anywhere in the database."""

    def test_no_refusal_anywhere_creates_a_league_season(self):
        for label, store in _backends():
            with self.subTest(backend=label):
                api = ApiService(store)
                pid, s1, gold = _program_season_league(api, "P", "S1", "Gold")
                s2 = api.create_season(pid, "S2")["id"]
                s3 = api.create_season(pid, "S3")["id"]
                anchor = api.create_league(s2, "Anchor")["id"]
                loose = api.create_league(s1, "Loose")["id"]
                _unbind(store, loose, s1)
                api.setup.create_league_season(gold, s3)   # Gold: S1 AND S3
                svc = ContextService(store)
                svc.set_with_league("u1", *ADMIN, pid, s2, anchor)

                before, bindings = _row(store), _bindings(store)
                attempts = (
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, loose),
                    # Ambiguous: Gold binds S1 and S3, current Season is S2.
                    lambda: svc.set_with_league("u1", *ADMIN, pid, s2, gold),
                    lambda: svc.set_with_league(
                        "u1", *ADMIN, pid, s2, "league_does_not_exist"),
                )
                for attempt in attempts:
                    self.assertEqual(self._refusal(attempt)[0], REFUSED, label)
                self._assert_inert(store, before, bindings, label)
                self.assertEqual(_bindings(store), bindings, label)
                _close(store)


if __name__ == "__main__":
    unittest.main()
