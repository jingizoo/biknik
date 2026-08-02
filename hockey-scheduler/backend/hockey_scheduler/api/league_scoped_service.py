"""League-scoped API facade extension (#173 PR C).

Draft generation is scoped in ``services.league_scoped_scheduler``. This facade
persists the resolved season context and revalidates draft commit/publish so a
stale or legacy row cannot bypass the same league-ice invariant.
"""

from datetime import datetime

from ..domain import Game, IceSlotStatus
from ..domain.errors import ConcurrencyConflictError, DomainError, ValidationError
from ..services.scheduler import (
    _existing_pairing_games,
    pairing_accounted_counts,
    unaccounted_pairing_row,
)
from ..services.league_scoped_scheduler import season_candidate_rink_ids
from ..services.league_scope import (
    require_game_league_id,
    require_slot_belongs_to_season,
    require_slots_belong_to_locked_season,
)
from .service import ApiService as _BaseApiService
from .service import catch


class ApiService(_BaseApiService):
    """API facade with league-isolated draft persistence and publishing."""

    def _draft_game_dto(self, game) -> dict:
        row = super()._draft_game_dto(game)
        row["season_id"] = game.season_id
        try:
            row["league_id"] = require_game_league_id(self.store, game)
        except DomainError:
            row["league_id"] = None
        return row

    def _draft_review_row(self, game, slot_games: dict,
                          double_booked: bool, policy_cache=None) -> dict:
        row = super()._draft_review_row(game, slot_games, double_booked,
                                        policy_cache=policy_cache)
        try:
            require_game_league_id(self.store, game)
            require_slot_belongs_to_season(
                self.store, game.ice_slot_id, game.season_id)
        except DomainError as exc:
            reason = getattr(exc, "details", {}).get("reason")
            issue = (
                "wrong_league_ice"
                if reason == "venue_access_missing"
                else "league_scope_missing"
            )
            if issue not in row["issues"]:
                row["issues"].append(issue)
        return row

    def _commit_draft_schedule_attempt(self, division_id: str = None,
                                       season_id: str = None,
                                       league_id: str = None,
                                       slot_ids=None, constraints=None,
                                       draft_fingerprint: str = None,
                                       actor_id=None) -> dict:
        """One attempt of the base facade's ``commit_draft_schedule`` retry
        shell (#318) — the shell (with ``@catch``) is inherited; overriding
        the attempt keeps the league-scoped body inside the retry loop, and
        this method must stay UNdecorated so a ``placement_raced`` reaches
        the shell as an exception, not a serialized error dict.

        Accepts the same Division-only or Season+League(+optional
        Division) scope as ``draft_season_schedule`` (#233 Slice G). Every
        created Game's ``season_id``/``league_id`` (the canonical grouping
        League, ``store.get_league``) are stamped from the regenerated
        proposal, and ``division_id`` is read per-row — a League-wide draft's
        rows can span several Divisions (or none). Previously ``league_id``
        was never set on a committed draft game at all, silently defeating
        the stranding guards in ``assign_season_team_league``/
        ``move_division_to_league`` (#233 Slice G review) for every
        scheduler-created game.

        #328 review round 5 — ``draft_fingerprint`` must match the fresh
        regeneration below (``preview_stale`` if not, ``preview_required``
        if omitted): this override reimplements the whole commit body
        independently of the base facade, so it needs the identical
        preview-binding check. See the base facade's
        ``_commit_draft_schedule_attempt`` for the full rationale.
        """
        proposal = self.draft_season_schedule(
            division_id=division_id, season_id=season_id, league_id=league_id,
            slot_ids=slot_ids, constraints=constraints)
        if isinstance(proposal, dict) and proposal.get("error"):
            return proposal
        if draft_fingerprint is None:
            raise ValidationError(
                "Generate a preview before committing this schedule.",
                {"reason": "preview_required"})
        if draft_fingerprint != proposal.get("draft_fingerprint"):
            raise ConcurrencyConflictError(
                "This preview is out of date — a game may have been added, "
                "cancelled, or otherwise changed since you generated it. "
                "Generate a fresh preview and review it before committing.",
                {"reason": "preview_stale"})
        resolved_season_id = proposal["season_id"]

        # The Division-only proposal's own "league_id" is this tenancy
        # layer's frozen Program-scoped vocabulary (league_scope.py's
        # league_id_for_division -> season.program_id, see scope_bridge.py's
        # docstring) — NOT the canonical grouping League Game.league_id
        # actually stores. Resolve the CANONICAL league straight from the
        # Division for that path; the League-wide path already received the
        # canonical league_id as an explicit param, so its own proposal
        # value is correct as-is.
        if season_id and league_id:
            canonical_league_id = proposal["league_id"]
        else:
            division = self.store.get_division(division_id) if division_id else None
            # #283: Division.league_id dropped; resolve its owning League via
            # the LeagueSeason it hangs off.
            division_ls = (self.store.get_league_season(division.league_season_id)
                           if division and division.league_season_id else None)
            canonical_league_id = division_ls.league_id if division_ls else None

        created = []
        # #159 — lock the target Season read-only and do EVERY Game/audit write
        # in one transaction, so a concurrent archive cannot slip between the
        # guard and the writes (autocommit would drop the FOR UPDATE lock at the
        # end of the check) and the batch stays all-or-nothing.
        with self.store.transaction():
            # #277/#313/#318 — the global Program→Team→Rink→Season lock
            # order, exactly as the base facade: Program rows (policy scopes
            # the per-row gate reads) FIRST — matching the ice-availability
            # builder's Program→Rink→Season — then teams, rinks, and every
            # involved Season in one sorted batch, with the pre-lock scope
            # locator re-verified under the locks. See
            # SetupService._policy_scope_lock_plan / _lock_teams /
            # _lock_rinks for the ordering contract.
            # #328 review round 13 -- the candidate Rink set is not just the
            # rinks of THIS proposal's placed rows: when the caller omits
            # slot_ids (the real Scheduler UI's own call), draft_season_schedule
            # considers every Rink with active SeasonVenueAccess for this
            # Season (`season_candidate_rink_ids`, computed the same way,
            # Rink-first so a Rink with ZERO existing slots is still
            # included) -- so an eligible-but-currently-unplaced Rink
            # gaining its first or another usable slot, having a candidate
            # slot allocated, or having its effective policy change is
            # invisible to a lock plan built only from placed rows.
            # Recomputing the SAME candidate pool the generator itself
            # scans, not just its output, keeps the locked set and the
            # generator's read set identical by construction -- current and
            # future, not one more hand-picked dimension.
            _pre_rinks = season_candidate_rink_ids(
                self.store, resolved_season_id, slot_ids)
            _plan = self.setup._policy_scope_lock_plan(
                _pre_rinks, (resolved_season_id,))
            self.setup._lock_programs(_plan["programs"])
            # #328 review round 8/9 -- also lock every already_scheduled
            # row's Teams, not just draft_games': the revalidation below
            # (whether that row's existing_game_id is still the current
            # non-cancelled Game for its pairing) is only genuinely
            # race-free, not just incidentally so, if a concurrent write
            # touching one of those Teams (a cancel, a re-pairing) is
            # forced to serialize against this transaction the same way
            # draft_games' own teams already are.
            self.setup._lock_teams(
                t for row in proposal["draft_games"] + proposal["already_scheduled"]
                for t in (row["home_team_id"], row["away_team_id"]))
            # #328 review round 13 -- recomputed fresh (not reused from
            # `_pre_rinks` above) under the Team locks just acquired, exactly
            # mirroring the pre-existing draft_games-only pattern this
            # replaces: the candidate pool itself, not just which rows placed
            # against it, can have changed in the gap since the locator read.
            _batch_rinks = season_candidate_rink_ids(
                self.store, resolved_season_id, slot_ids)
            self.setup._lock_rinks(_batch_rinks)
            self.setup._lock_seasons(_plan["seasons"])
            self._guard_active_seasons([resolved_season_id])
            self.setup._verify_policy_scope_plan(
                _plan, _batch_rinks, season_ids=(resolved_season_id,))
            # #328 review round 14 -- _batch_rinks above is still computed
            # and locked BEFORE the Season lock (Program->Team->Rink->Season
            # order), so a concurrent grant_season_venue_access (which takes
            # only the Season lock -- see SetupService.grant_season_venue_access)
            # can make a new Rink season-eligible in the exact gap between
            # that computation and the Season lock just acquired above.
            # That Rink's row was never in _batch_rinks and so was never
            # locked by _lock_rinks -- a concurrent create_ice_slot (which
            # takes only a Rink lock) can then give it its first candidate
            # slot at any point up to and including after the locked-regen
            # fingerprint recheck below (which sees no change while that
            # Rink still has zero slots) but before this commit's writes
            # complete. Re-verify the candidate set now that the Season
            # lock closes that gap for good: from here on our own Season
            # lock blocks any further grant, so any drift here proves the
            # inventory changed strictly before we started holding it, and
            # the whole attempt must restart against a fresh lock plan
            # rather than proceed against a scope we never actually locked.
            if season_candidate_rink_ids(
                    self.store, resolved_season_id, slot_ids) != _batch_rinks:
                raise ConcurrencyConflictError(
                    "A scheduling-policy scope changed while processing "
                    "the request; please retry.",
                    {"reason": "placement_raced"})
            # Resolve the exact LeagueSeason after the Season lock. A regular
            # draft must carry this identity so publish/move can enforce the
            # same competition scope, including league-wide rows with no
            # Division.
            draft_ls = self.store.league_season_for(
                canonical_league_id, resolved_season_id
            ) if canonical_league_id else None
            if draft_ls is None:
                raise ValidationError(
                    "The draft's League is not linked to this Season.",
                    {"reason": "draft_league_season_missing",
                     "league_id": canonical_league_id,
                     "season_id": resolved_season_id})
            draft_ls_id = draft_ls.id
            # #328 review round 12 finding 1 -- #314's participation check
            # and round 11's general fingerprint recheck (both below) must
            # run AFTER `_existing_now`, not before: with
            # `_require_batch_team_participation` first (the original
            # order), a team unregistering in the exact race window round 11
            # closed surfaced as `team_not_registered` -- a
            # DivisionMismatchError reason app.js's stale-preview recovery
            # does not recognise, leaving the operator a generic toast with
            # no clear-preview/refocus UX, instead of the terminal,
            # recoverable `preview_stale` every OTHER cause of staleness in
            # this same window produces. Symmetrically, a winning
            # exact-pairing race landing in that window surfaced as the
            # general recheck's generic `preview_stale` instead of the
            # specific, product-confirmed `pairing_already_scheduled` naming
            # the pairing and winning Game -- a regression against round
            # 2/3/4's own accepted contract for that exact scenario, which
            # this reordering also restores.
            # #206 slice 1 — freshly computed HERE, under the Team locks just
            # acquired, so the read is race-free against any other writer
            # touching these exact teams. Checked per row BELOW, BEFORE that
            # row's own slot/team-overlap check runs (#328 review round 4 —
            # reversed from the original design): a row whose exact pairing
            # already has a real Game is a terminal, product-confirmed fact
            # that must win regardless of whether the SAME row would ALSO
            # fail the physical check — the winning Game happens to sit on
            # this row's own slot, or a slot that overlaps it — because
            # `pairing_already_scheduled` names the specific pairing and
            # winning Game, which the physical-feasibility diagnoses
            # (`team_overlap`, `slot_unavailable`) cannot. This is a residual
            # check for what those checks structurally can't see either way
            # — a genuinely free, non-conflicting slot proposed for a
            # pairing that already has a real Game elsewhere, created by a
            # concurrent write between this proposal's generation and this
            # commit (the proposal already excluded pairings that existed AT
            # PREVIEW time — scheduler.py's already_scheduled split — so
            # only such a race can slip one through here). An UNRELATED
            # pairing's physical conflict is not in this index for that
            # row's own key, so it still falls through to the unchanged
            # physical check below. #328 review round 2 — this is a TERMINAL
            # fact, not transient contention: the operator reviewed a
            # specific proposal, and a winning commit already changed what
            # "missing" means, so silently substituting a different pairing
            # into the SAME commit would diverge from what was reviewed. The
            # batch rolls back atomically (raising inside this transaction)
            # and the caller must regenerate and re-review before retrying —
            # `pairing_already_scheduled` is deliberately NOT
            # `placement_raced` (the base facade's inherited retry shell
            # only retries that one reason), so this reaches the caller
            # unretried. #328 review round 12 -- also reused below by the
            # general fingerprint recheck's winning-pairing carve-out, under
            # this identical snapshot.
            _existing_now = _existing_pairing_games(
                self.store,
                {(draft_ls_id, row.get("division_id"))
                 for row in proposal["draft_games"]}
                | {(draft_ls_id, a.get("division_id"))
                   for a in proposal["already_scheduled"]})
            # #328 review round 11 finding 2 -- the checks immediately below
            # only revalidate draft_games/already_scheduled row identity and
            # participation; a team's ELIGIBILITY changing in the narrow gap
            # between this method's own pre-lock regeneration/fingerprint
            # compare and the locks just acquired is invisible to all of
            # them, since a newly-registered (or newly-unregistered) team
            # need not touch any row actually in this batch to change what a
            # fresh preview would show. Regenerating the complete current
            # proposal HERE -- now that every lock a proposal's inputs depend
            # on (Program/Team/Rink/Season) is held -- and comparing its own
            # fingerprint against the one the operator's Generate call
            # actually returned is one general check covering every
            # fingerprint-bound dimension at once (current and future),
            # rather than hand-listing each one. A concurrent
            # register_team_for_season/unregister_team_from_season also
            # locks the Season row (require_active_season), so by the time
            # this transaction holds it, any such write has either already
            # committed (and this regeneration observes it) or is blocked
            # behind this transaction (and cannot land before the writes
            # below).
            _locked_proposal = self.draft_season_schedule(
                division_id=division_id, season_id=season_id,
                league_id=league_id, slot_ids=slot_ids,
                constraints=constraints)
            if _locked_proposal.get("draft_fingerprint") != draft_fingerprint:
                # #328 review round 12 finding 1 -- a mismatch here can be
                # fully explained by a winning exact-pairing race: one of
                # THIS proposal's own draft_games rows now has a real Game
                # for its exact pairing in `_existing_now` above, the same
                # terminal fact the per-row loop below independently
                # detects. When that's the case, raise the specific,
                # product-confirmed `pairing_already_scheduled` (naming the
                # pairing and winning Game) instead of the generic
                # `preview_stale`, matching round 2/3/4's accepted contract
                # for this scenario exactly rather than silently downgrading
                # it just because this general recheck now runs first.
                #
                # #375 -- count, not membership (see the identical guard in
                # api/service.py). Meeting 2 of a pair is proposed while
                # meeting 1 legitimately exists, so a membership test would
                # misreport every ordinary `preview_stale` here as a pairing
                # race. Shared helper so the two facades cannot drift.
                _raced = unaccounted_pairing_row(
                    proposal["draft_games"], proposal["already_scheduled"],
                    draft_ls_id, _existing_now)
                if _raced is not None:
                    _raced_row, _raced_gid = _raced
                    raise ConcurrencyConflictError(
                        f"{_raced_row['home_team_name']} vs "
                        f"{_raced_row['away_team_name']} is already "
                        f"scheduled as Game {_raced_gid} — generate a "
                        "fresh preview before committing again.",
                        {"reason": "pairing_already_scheduled",
                         "home_team_id": _raced_row["home_team_id"],
                         "away_team_id": _raced_row["away_team_id"],
                         "existing_game_id": _raced_gid})
                raise ConcurrencyConflictError(
                    "This preview is out of date — a game may have been "
                    "added, cancelled, or otherwise changed since you "
                    "generated it. Generate a fresh preview and review it "
                    "before committing.",
                    {"reason": "preview_stale"})
            # #314 review — also re-validate every proposed row's competition
            # participation HERE, under the same locks: a concurrent
            # unregister_team_from_season or a team-to-league transfer can
            # commit in the SAME gap a stale pre-lock proposal would miss, after
            # which the write would persist a Game for a team no longer a valid
            # participant. Reuses the identical check create_game enforces.
            # #328 review round 12 finding 1 -- now redundant-but-harmless
            # defense-in-depth: the general fingerprint recheck above already
            # classifies any such change as `preview_stale` (or the more
            # specific `pairing_already_scheduled`) first, since a changed
            # participant is itself part of what the regenerated proposal's
            # fingerprint binds (team_ids / unschedulable_teams, round 10).
            # Left in place on the same established precedent as the other
            # narrower checks below: more specific where it can still add
            # anything, harmless where it can't.
            self.setup._require_batch_team_participation(
                resolved_season_id, draft_ls_id, proposal["draft_games"])
            # #314 review — re-validate every proposed slot's Season
            # eligibility HERE, under the Rink+Season locks just acquired,
            # not before this transaction opened (a stale prevalidation
            # could leave a window for a concurrent SeasonVenueAccess
            # revoke or Rink→Venue reassignment to commit and then be
            # missed) — shared with move_game via the same locked helper.
            # #328 review round 15 finding 2 -- also moved to run AFTER the
            # general fingerprint recheck above (previously ran right after
            # draft_ls_id resolution, before it): this narrower check's own
            # terminal reason (`venue_access_missing`) is not one of the
            # Scheduler UI's recognised stale-preview recovery reasons, so
            # the exact race this call guards against must not reach the
            # operator as this narrower, unrecognised reason before the
            # general recheck gets a chance to classify it as
            # `preview_stale` first — the identical reordering rationale
            # round 12 finding 1 already applied to
            # `_require_batch_team_participation` above. Any slot-scope
            # drift this call could still catch necessarily also changes
            # the Season-scoped candidate pool `_locked_proposal`'s own
            # regeneration draws from (`_season_scoped_slot_ids` runs the
            # identical `require_slot_belongs_to_season` check, directly
            # for an explicit `slot_ids` selection or via
            # `slot_belongs_to_season` for an omitted one) — already caught
            # above, leaving this now redundant-but-harmless
            # defense-in-depth, on the same established precedent as the
            # check above it.
            require_slots_belong_to_locked_season(
                self.store, [row["ice_slot_id"] for row in proposal["draft_games"]],
                resolved_season_id)
            # #328 review round 8 finding 1 -- an already_scheduled row's
            # Game is not part of this batch's writes, so nothing else ever
            # re-examines it under the lock: the wide draft_fingerprint gate
            # above only catches a cancellation that already happened by the
            # time THIS method's own regeneration ran, and the per-row loop
            # below only rechecks pairings that are actually in the batch.
            # If the blocking Game for an already_scheduled row is cancelled
            # in the narrow gap between that regeneration/fingerprint compare
            # and here, this batch is what the operator reviewed BEFORE that
            # cancellation -- it no longer reflects reality (that pairing is
            # now genuinely open too), and committing it anyway would persist
            # an incomplete schedule with no error. Reusing the SAME
            # `_existing_now` snapshot the per-row loop below relies on keeps
            # this race-free against the same locks; a mismatch means the
            # reviewed premise "this pairing already has Game G" no longer
            # holds, so refuse before any write exactly like any other form
            # of staleness.
            #
            # #375 -- ordered PREFIX per pairing, not one id per pairing (see
            # the identical guard in api/service.py).
            _expected_existing = {}
            for a in proposal["already_scheduled"]:
                _as_key = (draft_ls_id, a.get("division_id"),
                           frozenset((a["home_team_id"], a["away_team_id"])))
                _expected_existing.setdefault(_as_key, []).append(
                    a["existing_game_id"])
            for _as_key, _reviewed_ids in _expected_existing.items():
                _now_ids = list(_existing_now.get(_as_key) or ())
                if _now_ids[:len(_reviewed_ids)] != _reviewed_ids:
                    raise ConcurrencyConflictError(
                        "This preview is out of date — a game may have "
                        "been added, cancelled, or otherwise changed since "
                        "you generated it. Generate a fresh preview and "
                        "review it before committing.",
                        {"reason": "preview_stale"})
            # #375 -- per-pairing meetings the reviewed proposal accounted for.
            _accounted = pairing_accounted_counts(
                proposal["already_scheduled"], draft_ls_id)
            for row in proposal["draft_games"]:
                # #328 review round 4 -- checked BEFORE the physical gate
                # below, not after: a row whose pairing already has a real
                # Game is a terminal, product-confirmed fact (#326) that
                # must win regardless of whether the SAME row would also
                # fail the physical check (e.g. the winning Game happens to
                # sit on this row's own slot, or a slot that overlaps it) —
                # `pairing_already_scheduled` names the specific pairing and
                # winning Game, which `slot_unavailable`/`team_overlap`
                # cannot. An UNRELATED pairing's physical conflict (a
                # different pairing merely sharing one team, or an
                # unrelated slot collision) is not in `_existing_now` for
                # THIS row's key and so still falls through to the
                # unchanged physical check below.
                #
                # #375 -- count, not membership (see api/service.py). At N=1
                # `_accounted` is 0 for any drafted pairing, making this
                # literally the old `key in _existing_now`.
                _pairing_key = (draft_ls_id, row.get("division_id"),
                                frozenset((row["home_team_id"], row["away_team_id"])))
                _consumed = _accounted.get(_pairing_key, 0)
                _accounted[_pairing_key] = _consumed + 1
                _existing_ids = _existing_now.get(_pairing_key) or ()
                if _consumed < len(_existing_ids):
                    # #328 review round 3 -- the message itself (not just
                    # details) must be actionable: post()'s generic toast in
                    # app.js surfaces error.message alone, never
                    # error.details, so a vague message here would leave the
                    # operator with no idea which pairing/Game raced. The
                    # proposal row already carries both team names.
                    _existing_gid = _existing_ids[_consumed]
                    raise ConcurrencyConflictError(
                        f"{row['home_team_name']} vs {row['away_team_name']} "
                        f"is already scheduled as Game {_existing_gid} — "
                        "generate a fresh preview before committing again.",
                        {"reason": "pairing_already_scheduled",
                         "home_team_id": row["home_team_id"],
                         "away_team_id": row["away_team_id"],
                         "existing_game_id": _existing_gid})
                # #277: run the SAME final conflict check as create_game /
                # move_game before persisting — slot free (exists, GAME,
                # AVAILABLE, not already held) AND neither team on an overlapping
                # fixture — so a regenerated proposal that would double-book a
                # slot OR a team fails atomically (the whole batch rolls back)
                # instead of silently persisting a bad fixture. Per #277's
                # acceptance, schedule commits enforce the identical check as
                # manual moves; there is no draft-only exception. Returns the
                # resolved slot to allocate below. (The base facade's own
                # commit_draft_schedule uses this same check; this override
                # reimplements the commit body for league-scope validation, so it
                # must enforce the identical invariant.)
                slot = self.setup._assert_slot_free_for_game(
                    row["ice_slot_id"], row["home_team_id"], row["away_team_id"],
                    season_id=resolved_season_id)
                game = Game(
                    id=self.store.next_id("game"),
                    home_team_id=row["home_team_id"],
                    away_team_id=row["away_team_id"],
                    start_time=datetime.fromisoformat(row["start_time"]),
                    end_time=(datetime.fromisoformat(row["end_time"])
                              if row.get("end_time") else None),
                    rink=row.get("rink_name"),
                    season_id=resolved_season_id,
                    league_id=canonical_league_id,
                    division_id=row.get("division_id"),
                    ice_slot_id=row.get("ice_slot_id"),
                    league_season_id=draft_ls_id,
                    published=False,
                    is_draft=True,
                )
                self.store.add_game(game)
                # A committed draft occupies its ice: flip the backing slot to
                # ALLOCATED (exactly as create_game / move_game do) so later
                # scheduling and the shared checker read it as taken instead of
                # offering the same occupied ice again. The check above already
                # rejected any slot a game holds, so this only flips AVAILABLE.
                slot.status = IceSlotStatus.ALLOCATED
                self.store.save_ice_slot(slot)
                created.append(self._draft_game_dto(game))
            if season_id and league_id:
                scope_type, scope_id = "league", league_id
            else:
                scope_type, scope_id = "division", division_id
            self.setup._audit(
                "draft_schedule_committed", scope_type, scope_id, actor_id,
                {
                    "created_count": len(created),
                    "game_ids": [row["game_id"] for row in created],
                    "unscheduled_count": len(proposal["unscheduled"]),
                    "season_id": resolved_season_id,
                    "league_id": proposal["league_id"],
                },
            )
        return {
            "division_id": division_id,
            "season_id": resolved_season_id,
            "league_id": proposal["league_id"],
            "created": created,
            "unscheduled": proposal["unscheduled"],
        }

    @catch
    def publish_draft_games(self, game_ids=None, all_drafts=False,
                            actor_id=None) -> dict:
        targets = self._draft_targets(game_ids, all_drafts)
        # Validate the whole batch before the first slot allocation or publish;
        # one bad legacy draft must not partially publish the selection.
        for game in targets:
            require_game_league_id(self.store, game)
            require_slot_belongs_to_season(
                self.store, game.ice_slot_id, game.season_id)
        return super().publish_draft_games(
            game_ids=game_ids, all_drafts=all_drafts, actor_id=actor_id)
