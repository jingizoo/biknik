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
"""

# Reuses today's ``_LIFECYCLE_GATE_KEY`` string (``web/server.py``) for
# continuity: it already means "the one shared, global epoch-affecting
# concern" in this codebase, and every caller migrating off the old gate's
# constant onto this one keeps the identical wire/log value.
EPOCH_FENCE_GLOBAL_KEY = "lifecycle"


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
