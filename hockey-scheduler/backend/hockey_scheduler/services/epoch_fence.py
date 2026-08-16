"""Key-naming for the database-coordinated epoch fence (PR #423 redesign).

ONE mechanism, two key granularities — exactly mirroring the pre-existing,
owner-accepted split between ``CONTEXT_GATE`` (per-user) and ``LIFECYCLE_GATE``
(one global key) in ``services/context_gate.py``, which this module's keys feed
into the fence's store-layer primitive (``SqlStore``/``InMemoryStore``
``epoch_fence_acquire_exclusive``/``epoch_fence_acquire_shared``) rather than a
Python condition variable.

* A **per-user** key (:func:`user_fence_key`) — for writers that already hold
  the affected user's own id (context switch, account scope rebind,
  account activate/deactivate).
* **One global** key (:data:`EPOCH_FENCE_GLOBAL_KEY`) — for writers whose
  affected user(s) can only be found by a lookup the writer would have to
  perform, or whose effect is inherently shared (Season/Program/League/
  LeagueSeason lifecycle and deletion, venue-access grant lifecycle, Team
  transfer, Official assign/unassign, Player/Guardian reassignment).

A scoped read acquires **both** keys shared, so it is ordered against both
writer classes — see ``web/server.py``'s ``_read_under_context_gate``.

This module intentionally holds ONLY the key-naming convention, not the fence
mechanism itself (that lives on the store, per backend — see
``SqlStore.epoch_fence_acquire_exclusive``/``_shared`` and the same methods on
``InMemoryStore``), so every caller across ``web/server.py``,
``context_service.py``, ``account_service.py``, ``setup_service.py`` and
``guardian_service.py`` names a key the exact same way, with one source of
truth for the string format.

KNOWN, ACCEPTED CONSEQUENCE -- discovered during implementation, not merely
theorized, so recorded here where the next reader of the global key will find
it: because ``EPOCH_FENCE_GLOBAL_KEY`` is EXCLUSIVE-locked by all FOURTEEN
"global" writers (season/program/league/league_season lifecycle+delete,
venue-access revoke/delete, Team transfer, Official assign/unassign,
Player/Guardian reassignment) for their WHOLE transaction, any two of those
fourteen now serialize against EACH OTHER, not merely against a concurrent
scoped READ (the fence's actual purpose -- see the module docstring above).
Before this widening, the ONLY thing sharing this global key
(``_LIFECYCLE_GATE_KEY``/``LIFECYCLE_GATE``) was Season archive/reopen, a
rare, admin-driven, effectively single-flight operation, so writer-vs-writer
contention on it was never practically observable. Two `unassign_official`
calls for two COMPLETELY UNRELATED games now briefly serialize where they
never did before -- confirmed directly (not merely reasoned about) via
``tests/test_write_race_hardening.py``'s
``test_unassign_vs_unassign_single_effect``, a pre-existing, PR-#423-adjacent
test whose original assertions pinned the OLD, purely-row-lock-mediated
resolution; PR #423 updates that test's specific pinned outcome (which side
wins, what error the loser sees) while preserving, and re-verifying just as
strictly, the safety property it exists to prove (exactly one effect, no
corruption, a well-typed retryable error for the loser, not a hang or a wrong
answer) -- see that test's own updated docstring for the full account. This
is judged an acceptable, DELIBERATE consequence of the owner's own explicit
choice to fold all fourteen into one global key (design §4.1/§4.2, §11.4's
open question read in the direction of "one mechanism, coarse membership"),
not a defect being silently absorbed -- flagged here, and in the PR's own
reporting, exactly so a future reader does not mistake newly-discovered
writer-vs-writer contention for an oversight. Ordinary (non-test-paused)
production contention on this key is expected to be on the order of the
transaction time of ONE of these fourteen writers (milliseconds), not the
fence's full timeout bound -- this test's use of an artificial pause is what
turns a normally-brief overlap into the full bounded wait, exactly the way
this codebase's other ``_pause_race``-style tests already turn other brief
races into deterministic, observable ones.
"""

# Reuses today's ``_LIFECYCLE_GATE_KEY`` string (``web/server.py:601``,
# ``"season-lifecycle"``) verbatim, for continuity: it already means "the one
# shared, global epoch-affecting concern" in this codebase, and every caller
# migrating off the old gate's constant onto this one keeps the identical
# wire/log value. tests/test_epoch_fence.py asserts this against
# ``web/server.py``'s own constant directly, so the two cannot silently drift.
EPOCH_FENCE_GLOBAL_KEY = "season-lifecycle"


def user_fence_key(user_id) -> str:
    """The per-user fence key for ``user_id``.

    A falsy ``user_id`` is not meaningful here (mirrors
    ``ContextSwitchGate``'s existing falsy-``user_id`` short-circuit,
    ``context_gate.py:249-252``): such a caller (the identity-less X-Demo-Role
    fallback) owns no context row and has nothing to be ordered against, so
    callers check for a falsy ``user_id`` themselves before acquiring this key
    rather than this function special-casing it.
    """
    return f"user:{user_id}"
