"""THE CONTEXT EPOCH: a server-issued, non-evictable token that says *which
selection a scoped read was rendered under*, so a read dispatched before a
context switch cannot be evaluated as an independent read taken after it
(#159 follow-up to #415).

THE RESIDUE #415 LEAVES, and why it is not a defect in #415. ``context_gate.py``
orders participants BY ARRIVAL AT ``do_GET``. ``app.js`` orders them BY FETCH
DISPATCH. Between those two instants sits an interval that neither process owns
— the browser's own request queue and the wire:

    render() dispatches  GET /api/v2/setup/seasons/season_3/venue-candidates
    the operator switches; cancelContextScopedReads() aborts it and the JS
      promise SETTLES, so awaitContextScopedReadSettlement() returns
    POST /api/context arrives FIRST, takes the gate's writer slot, commits
    the GET finally reaches do_GET and takes a HIGHER arrival sequence

At that point every component is correct. The read arrived after the writer, so
the gate correctly makes it wait behind that writer and correctly runs it
against the NEW tuple; the unchanged exact-Season ceiling then compares
``season_3`` against the new selection and answers the generic 404 — for a
question the operator had already withdrawn. No wait expires and no
``[context-gate]`` line is logged.

    CI RECORD: main@1de50d7 and main@e385bfb both fail browser shard 1, journey
    ``setup-state-matrix``, DESKTOP leg, on exactly that URL, while ``app.js``'s
    own ledger records the read as ``generation=5 dispatched=true``.

A CLIENT ABORT IS NOT A TRANSPORT CANCELLATION. ``AbortController.abort()``
settles the JS promise; it does not un-send a request already on the wire and
cannot stop the server answering one it has not yet read. So the fact the server
is missing — *the read was asked under a selection that no longer exists* — has
to reach the server on the request itself.

WHY THIS IS AN EPOCH AND NOT A CANCELLATION LEDGER. The obvious design is the
one this module REPLACES: give each read an opaque id, have the switch's POST
declare the ids it cancelled, and keep those ids in a bounded per-user registry
that a late arrival consults. The repository owner rejected it, and the ruling
is the specification for this module:

    "Eviction/TTL must never turn a declared cancellation back into a 404. 'TTL
     exceeds the gate wait' is insufficient because network arrival can exceed
     both. The design needs proof that retention lasts through claim, or fail
     the switch before committing, or use non-evictable epoch/tombstone
     semantics."

The rejection is exact rather than stylistic. Any registry has to answer "is
this id still here?", and every way an id can leave — claimed, expired by TTL,
evicted by a per-user cap, evicted by a per-process cap — collapses to the same
answer, which falls through to the ceiling and back to the 404 the mechanism
existed to prevent. A read's arrival time is bounded by the NETWORK, not by
anything this process controls, so no retention window can be proven long
enough. **The only sound fix is to retain nothing.**

    NOTHING IS RETAINED HERE. There is no registry, no TTL, no cache, no
    eviction and no configuration. The epoch is DERIVED, on demand, from
    persisted state (see WHAT THE MATERIAL IS below). An arrival delayed by an
    hour is compared exactly as correctly as one delayed by a millisecond,
    because the comparison reads the same persisted state either way. The
    rejected failure mode is UNCONSTRUCTIBLE, not merely unlikely.

THE MECHANISM, in three sentences. The server derives an opaque token and hands
it out wherever the client already learns its context (``GET /api/context``,
``GET /api/context/options``, and the ``POST /api/context`` response). Each
context-scoped GET echoes, in ``X-Context-Epoch``, the token it was RENDERED
UNDER. On arrival the server compares that echo with the token derived from
persisted state as it stands NOW: equal means the selection has not moved and
the request proceeds exactly as it does today, ceiling included; unequal means
the tuple moved while the request was in transport, and it is answered ``204 No
Content`` before the ceiling is ever evaluated.

WHAT THE MATERIAL IS, and why it is not the raw ``ActiveContext`` row (#159
review finding 2). An earlier revision hashed the PERSISTED ROW directly —
``store.get_active_context(user_id)``'s own fields. That is a change detector
for the operator's SAVED selection, and it is the wrong question: the payload
the client actually renders is the EFFECTIVE resolution
(``ContextService.resolve_with_league``), which can differ from the saved row
whenever the saved Program/Season is deleted or no longer authorized (a
deterministic fallback stands in, and the row is deliberately NOT rewritten —
see ``ContextService``'s own docstring) — and the gap is not academic:

    Season S1 is persisted as the saved selection. S1 is deleted (or its
      authorization is withdrawn) -> resolve() now falls back to S3, which the
      client renders and whose epoch is issued
    S3 is ARCHIVED -> resolve()'s fallback now picks S2 instead. The RAW ROW
      never changed (it still names S1, which still fails to resolve either
      way), so a token derived from the row alone is BYTE-IDENTICAL before and
      after S3's archive
    a scoped read rendered under the S3 epoch arrives, echoing a token that
      still "matches" — the row never moved — and is judged against S2's
      ceiling: a 404 for a Season the caller was never shown, instead of the
      204 discard the moved EFFECTIVE selection earned it

So the material is the EFFECTIVE ``(program, season, league)`` tuple
``resolve_with_league`` would render for this exact ``(user_id, role, scope)``
— see ``ContextService.resolve_epoch_state`` / ``_epoch_material_locked``,
which read it under the same snapshot machinery every other resolution in that
service uses — plus the persisted switch GENERATION (next paragraph) and the
effective Season's lifecycle (see WHY THE SEASON'S LIFECYCLE below). This
module itself stays store-free: it hashes objects it is HANDED, and resolving
them is ``ContextService``'s job, not this one's — see WHAT THIS MODULE DOES
NOT DO.

FOUR PROPERTIES, each of which this module has to earn:

  1. STABLE FOR A GIVEN INPUT. The same ``(user_id, generation, program,
     season, league)`` hashes to the same token every time, in every process,
     across restarts — the material is exactly those objects' ids/fields, and
     nothing else. Deliberately NOT a per-process counter or a random nonce:
     either would make every restart invalidate every outstanding read, and
     would make the token mean "which process answered" rather than "which
     selection".
  2. DIFFERENT AFTER ANY SWITCH, INCLUDING A SWITCH BACK TO THE SAME TUPLE
     (#159 review finding 5). ``ContextService.set``/``set_with_league`` read
     the CURRENT persisted generation and write current+1 on EVERY commit —
     inside the same serializable transaction as the write, so two concurrent
     writers correctly serialize into two distinct successive values rather
     than racing a lost update. A -> B -> A therefore moves the generation
     TWICE and the token twice, even though the EFFECTIVE tuple ends where it
     began.

       NOT WALL-CLOCK ``updated_at``, which an earlier revision used instead
       and which the review correctly rejected: two commits landing inside the
       same tick (a coarse system clock, load, a virtualized host) can share a
       timestamp, and A -> B -> A could then reuse an epoch — silently
       readmitting a read from before the round trip, which is precisely the
       non-evictable-cancellation guarantee this module exists to uphold. A
       counter that is READ then WRITTEN ONE HIGHER, inside one transaction,
       has no such window: it does not matter whether two writes share a
       microsecond, only that each one reads what the other already committed
       or loses a serialization conflict and retries.
  3. OPAQUE, NON-IDENTIFYING, AND — new in this revision — UNFORGEABLE WITHOUT
     A DEPLOYMENT SECRET (#159 review finding 4). The material is HASHED, never
     concatenated or encoded, so no user id, Program id, Season id, League id
     or generation can be READ OUT of a token; the digest is additionally
     KEYED (see :func:`epoch_secret`), so it also cannot be RECOMPUTED —
     confirming a guess, or enumerating a whole low-entropy candidate space —
     by a party who does not hold that secret. See NON-DISCLOSURE below for
     both halves of that claim, precisely.
  4. FAILS CLOSED. A token that is absent behaves exactly as today (no
     behaviour change for any client that does not send one); a token that is
     present but malformed, or present and simply wrong, DISCARDS. There is no
     input that causes data to be served which would otherwise be refused.

IT CONFERS NO AUTHORITY, which is the property the whole design must be read
against. The only thing a comparison can produce is "discard this request".
  * Echo a STALE token and you discard your OWN read — nobody else's, because
    the token is compared against the epoch of the ``user_id`` resolved from
    the SESSION, and nothing in this module ever reads an identity out of a
    token, a header or a body.
  * Echo the CURRENT token on a genuinely stale question and you get today's
    404, unchanged: matching only means the ceiling is allowed to run.
  * Echo garbage and you discard your own read.
  So the header can never widen scope, never identify, never authorize, and
  never serve a byte the ceiling would have refused. It is strictly a way to
  throw your own request away.

NON-DISCLOSURE, stated precisely rather than rounded up. The token confers no
AUTHORITY either way (see above) — but a party who holds ONLY a leaked token
and does not hold the deployment secret must also be unable to CORRELATE it
back to an identity, which is a property the material's shape alone cannot
buy for a low-entropy id space (#159 review finding 4). A public,
non-identity-bearing sentinel and a small sequential ``user_id`` space (an
early installation's accounts, or any deployment that mints ids sequentially)
is a dictionary of a few hundred candidates; an UNKEYED digest is a pure
function of that material, so hashing every candidate and comparing against a
leaked token recovers the identity with no authority gained and no request
sent — exactly the attack the fixed
``test_a_leaked_token_cannot_be_correlated_to_an_identity_without_the_
deployment_secret`` case mounts and confirms fails against this module.
``hashlib.blake2s``'s ``key`` therefore carries a deployment secret (see
:func:`epoch_secret`) THE SAME EVERY REPLICA HOLDS: the digest is a keyed MAC,
so recomputing it — confirming a guess, or enumerating a whole candidate space
— requires the secret, not merely the material. A party who already holds the
complete material (the effective tuple, the persisted generation, and the
selected Season's lifecycle) but NOT the secret learns nothing new by
recomputing, exactly as the keyless version intended; the difference is that
now nobody else can recompute it either. An epoch leaked into a log, a proxy
trace or a browser history entry therefore discloses no id, no tuple and no
generation, AND cannot be correlated back to the account that produced it
without the secret.

WHERE IT IS COMPARED, and why that placement is load-bearing in both
directions. There is no ``ContextService.run_scoped_read`` — that name
describes a revision that was BUILT AND REJECTED (see ``ContextService``'s own
NOTE on #159 review finding 3, ``services/context_service.py``): wrapping the
comparison and the dependent read in one ``store.transaction()`` holds the
store's process-wide lock across whatever ``produce()`` does, which measurably
DEADLOCKED a real test harness and would, in production, stall every other
request touching the store for the duration of one slow dependent read. The
comparison instead happens in ``web/server.py``'s
``Handler._read_under_context_gate``, which derives the CURRENT epoch and
compares it to the echo, and then — if and only if it matches or nothing was
echoed — calls the dependent read, ordered by GATES rather than a shared
database transaction:
  * ``Handler._context_read_hold`` (per-user) and ``_lifecycle_read_hold``
    (global) — the SAME in-process ``ContextSwitchGate`` shape #415/#159 built
    — are held across both the comparison and the dependent read, so a
    lifecycle mutation or a competing context switch that takes either gate
    cannot land BETWEEN them: either it is ordered wholly before (and the
    freshly-derived current epoch already reflects it, so a stale echo
    correctly mismatches) or wholly after (and the dependent read is judged
    against the exact state the comparison just matched) — never a hybrid. A
    post-service comparison is NOT equivalent: by the time it could run, the
    dependent read may already have made an authorization or privacy decision
    on stale data.
  * PR #423 LAYERS a database-coordinated fence alongside those two gates —
    ``STATE.api.store.epoch_fence_acquire_shared`` for the SAME per-user and
    global keys, real advisory-lock holds on PostgreSQL (the only backend an
    independent second process can exist on), documented no-ops on
    SQLite/Memory — widening the writers ordered against to the full set in
    ``services/epoch_fence.py``'s own table, and extending the guarantee
    ACROSS replicas rather than only within one process. A shared hold that
    could not be confirmed (a bounded PostgreSQL wait that timed out) is
    treated exactly like a mismatch — see that method's own docstring for the
    round-N review finding 2 fix.
  * A ROUND-N review finding 1 addition closes what the gates and the
    PostgreSQL-only advisory lock structurally cannot: a persisted version
    counter, keyed the SAME way, is sampled before the comparison and again
    after the dependent read returns, with NO LOCK held across that call at
    all (the AB-BA deadlock a real SQLite lock was measured to produce is
    exactly what this avoids). Every writer bumps it as part of its own
    transaction; if the two samples disagree, some writer's whole transaction
    landed inside the window regardless of backend, and the (already-computed)
    result is discarded rather than served.
  * BEFORE the service call in every case, so a discarded read never reaches
    ``api/service.py`` and the exact-Season ceiling is never evaluated for it.
    That is what lets the answer disclose nothing in EITHER direction — a
    discard looks identical whether the named Season is a sibling of the
    selection, is archived, or never existed.

THIS MODULE'S OWN TOKEN IS NOT PER-PROCESS, which is not quite the same claim
as "the mechanism that compares it is not per-process" — the gates above
honestly ARE per-process (they hold Python locks; see ``context_gate.py``'s
own HONEST SCOPE LIMIT), and that is exactly why PR #423 added the database
fence rather than declaring the gates sufficient. What THIS module's
``context_epoch()`` guarantees is narrower and unconditional: it is a pure
function of shared, persisted state (a database row, the objects it resolves
to, and a deployment-wide configured secret) with no in-memory component of
its own, so a token issued by replica A compares correctly on replica B and a
restart mid-flight changes nothing — PROVIDED the deployment secret
(:func:`epoch_secret`) is configured identically on every replica; see that
function's docstring for the rotation story, which is the one way this
property can be temporarily broken on purpose. Whether a STALE token gets
CAUGHT before a torn read is served is the comparison mechanism's job,
described above and in ``services/epoch_fence.py``/``web/server.py``, not
this module's.

WHY THE EFFECTIVE SEASON'S LIFECYCLE IS PART OF THE MATERIAL, and not only the
resolved tuple's ids. The token above answers "has the operator's EFFECTIVE
SELECTION moved". That is not the whole question the scoped reads are judged
by, and the gap was measured rather than reasoned about:

    the Setup hierarchy answers, reporting `read_only: false` for the selected
      Season, and the client decides — correctly, on that snapshot — to fetch
      its grant candidates
    the Season is ARCHIVED (a second tab, another operator, this operator)
    the candidate GET arrives; `season_is_read_only` is now true and the
      unchanged exact-Season ceiling answers its deliberate generic 404

    MEASURED: an archive writes ``Season.status``/``archived_at`` on the
    Season row itself, and the SAME Season object is still the effective
    selection either side of it (archived is honored as read-only history, not
    swapped out) — so the ids alone are BYTE-IDENTICAL before and after the
    archive. The epoch could not see the transition, so it could not discard
    the read, and the 404 survived the mechanism built to remove it.

So the material also carries the EFFECTIVE Season's lifecycle, asked through
``season_guard.season_is_read_only`` — the SAME predicate ``require_active_
season`` refuses writes on, that ``get_venue_grant_candidates`` refuses this
very read on, and that ``get_setup_hierarchy_v2`` publishes per Season as
``read_only``. ONE authority, consulted by the epoch and by the refusal, so
there is no third notion of "archived" to drift: whenever the refusal's answer
would change, the material it is computed from has changed, and the token has
therefore already moved.

    THE ORDERING THAT MAKES THIS WORK is in the callers, not here — every
    payload derives its epoch BEFORE reading the data it describes, so a
    lifecycle change landing mid-payload yields (new data, OLD epoch) and the
    follow-up read is DISCARDED. The other order would yield (old data, NEW
    epoch): admitted, then refused at the ceiling — the 404 back again. See
    ``Handler._with_context_epoch``.

The raw ``status`` and ``archived_at`` are hashed BESIDE that decision, which
is deliberately MORE than the refusal consults. It costs nothing and it buys
the reopen direction twice over: archive -> reopen -> archive returns the
decision to its starting value, and only ``archived_at`` distinguishes the two
archived states. A field that moves without the refusal moving can only cause a
DISCARD — a read thrown away and re-issued — never a serve, so erring wide here
is the safe direction.

    A ROUND TRIP RETURNS THE TOKEN TO WHERE IT STARTED, and unlike the tuple's
    A -> B -> A this is deliberate rather than tolerated. ``reopen_season``
    clears ``archived_at``, so active -> archived -> active is byte-identical
    at both ends and a read rendered before the archive is ADMITTED after the
    reopen. That is the right answer, not a hole: it is admitted to the exact
    ceiling it was rendered under, which now answers exactly what the render
    expected it to. The GENERATION axis differs because it moves on every
    CONTEXT WRITE (a switch), and archiving/reopening a Season is not one —
    there the two ends only look alike if you were also expecting a switch to
    have happened, and none did.

WHAT THIS MODULE DOES NOT DO, stated for the boundary rather than left
implicit. It never opens a transaction, never reads ``os.environ`` for
anything but the deployment secret, and never calls into ``ContextService`` or
a store. RESOLVING the effective tuple under one consistent snapshot (findings
2+3), and INCREMENTING the persisted generation on a write (finding 5), are
``ContextService``'s job (``resolve_epoch_state`` / ``_epoch_material_locked``
/ ``_next_generation_locked`` / ``current_epoch``) precisely so this module
can stay a pure function of whatever it is handed — testable without a store,
a transaction, or a thread, and incapable of quietly acquiring retention logic
of its own (see ``test_nothing_is_retained_so_nothing_can_be_evicted``).
Ordering the comparison against a dependent read — deciding WHEN it is safe to
call ``current_epoch`` and act on the answer — is ``web/server.py``'s
``Handler._read_under_context_gate``'s job, not this module's or
``ContextService``'s; see that method's own docstring (and the module
docstring's WHERE IT IS COMPARED section above) for the gates and the
database-coordinated fence that do it.
"""

import hashlib
import os
import re
from datetime import datetime
from enum import Enum

from .season_guard import season_is_read_only

__all__ = ["CONTEXT_EPOCH_HEADER", "EPOCH_ABSENT", "EPOCH_MATCH",
           "EPOCH_MISMATCH", "context_epoch", "epoch_secret", "epoch_verdict",
           "is_epoch_token"]

# The request header a context-scoped GET echoes its rendered-under epoch in.
# A HEADER rather than a query parameter on purpose: it is transport metadata
# about the request, not part of the question being asked, and putting it in the
# URL would change cache keys and leak into access logs for no benefit.
CONTEXT_EPOCH_HEADER = "X-Context-Epoch"

# 16 bytes -> 32 lowercase hex characters. The token only ever has to
# distinguish one persisted row from the next, so collision resistance far below
# this would do; 128 bits costs nothing on the wire and removes the question.
_DIGEST_BYTES = 16
_EPOCH_RE = re.compile(r"^[0-9a-f]{32}$")

# BLAKE2's personalization field, i.e. domain separation that is part of the
# algorithm rather than of the message. A CONSTANT, never a per-process value:
# property 1 above requires that the same input hash identically after a
# restart. EXACTLY 8 BYTES because blake2s rejects anything longer, and the
# trailing digit is a format version — if the material below ever changes
# shape, bump it rather than reusing this one, so old and new tokens cannot be
# confused for each other during a rollout.
#
# BUMPED TWICE. "1" -> "2" when the selected Season's lifecycle joined the
# material. "2" -> "3" (#159 review findings 2+5) when the material itself
# changed shape: the raw ``ActiveContext`` row's fields (id/program_id/
# season_id/league_id/updated_at) were REPLACED by user_id + the EFFECTIVE
# resolved tuple's ids + the persisted generation. A browser holding a "2"
# token would otherwise be compared against a "3" token computed from
# different material entirely; the two must be UNEQUAL rather than
# coincidentally equal, which bumping guarantees. Unequal is the safe answer —
# those in-flight reads discard once and are re-issued under the new token —
# whereas a reused personalization risks a coincidental collision nobody could
# predict. (Keying the digest with a deployment secret, the OTHER #159 review
# change in this file, did NOT need a bump: it changes who can recompute a
# token, not what the material is, and an old unkeyed token already fails to
# match a new keyed one by construction — a discard, the same safe outcome.)
_PERSON = b"hsctxep3"

# -- the deployment secret (#159 review finding 4) --------------------------
#
# Sourced the SAME WAY this repository's other deployment secrets are —
# ``services/passwords.py``'s ``APP_MODE``-conditional pattern and
# ``bootstrap.py``'s ``INITIAL_SETUP_CODE``: an environment variable, REQUIRED
# and validated in production, defaulted outside it so the stdlib-only,
# no-configuration test suite and local/demo runs keep working with zero setup.
_SECRET_ENV = "HS_CONTEXT_EPOCH_SECRET"

# A floor, not a target: 32 raw bytes is 256 bits before folding through
# ``_derived_key``, comfortably above what BLAKE2s's 32-byte keyspace can even
# use. Rejects a short/placeholder value outright rather than accepting it and
# quietly running with a weak key — the same shape as the PBKDF2-iteration and
# minimum-password-length floors in ``services/passwords.py``, which also never
# let a stray override weaken a production default.
_MIN_SECRET_BYTES = 32

# The demo/dev/test fallback key, used ONLY when APP_MODE is not "production"
# and HS_CONTEXT_EPOCH_SECRET is unset. Committed in the open, deliberately: it
# buys nothing by being secret — a demo/dev deployment has no real account
# population worth correlating, and anyone who can read this source file
# already has it. What it buys is that EVERY code path is keyed, never
# conditionally unkeyed while configuration is pending; see :func:`epoch_secret`
# for the production-only enforcement that makes this fallback unreachable
# where it would matter.
_DEMO_SECRET = (b"hs-context-epoch-demo-key-do-not-use-in-production-"
               b"9f3c2a7e1b5d4f6089ac")


def _is_production() -> bool:
    return (os.environ.get("APP_MODE") or "demo").strip().lower() == "production"


def epoch_secret() -> bytes:
    """The deployment secret :func:`context_epoch` keys its digest with.

    FAILS CLOSED rather than degrading to an unkeyed hash (#159 review
    finding 4's explicit fix requirement): a PRODUCTION process with
    ``HS_CONTEXT_EPOCH_SECRET`` unset, empty, or shorter than
    :data:`_MIN_SECRET_BYTES` raises immediately. Called eagerly at process
    startup (``web/server.py``'s ``serve()``), before the socket binds and
    before any request can be answered — a misconfigured production
    deployment must not boot at all, not merely 500 on its first
    context-scoped request. Every non-production environment (demo, dev, the
    test suite) gets the fixed, openly-documented :data:`_DEMO_SECRET` when
    the variable is unset, so nothing outside production requires setup.

    ROTATION. Changing the value invalidates every outstanding token: a
    request in flight when the secret rotates lands on
    :data:`EPOCH_MISMATCH` exactly as a genuinely stale epoch does — a
    ``204`` discard, never an error and never a wrong serve (the module's
    "confers no authority" property is unaffected by which key produced the
    comparison value). Deploy the SAME value to every replica: a token issued
    by one process must verify on another (see the module docstring's "PER
    PROCESS? NO" — this preserves that property rather than reintroducing a
    per-process concept), so rotating means updating the configured secret
    everywhere at once, not staggering it.
    """
    raw = os.environ.get(_SECRET_ENV)
    production = _is_production()
    if raw:
        secret = raw.encode("utf-8")
        if len(secret) < _MIN_SECRET_BYTES:
            raise RuntimeError(
                f"{_SECRET_ENV} is set but too short ({len(secret)} bytes; "
                f"{_MIN_SECRET_BYTES}+ required). Generate a high-entropy "
                f"value, e.g. `python3 -c \"import secrets; "
                f"print(secrets.token_hex(32))\"`, and deploy it identically "
                f"to every replica.")
        return secret
    if production:
        raise RuntimeError(
            f"{_SECRET_ENV} must be set when APP_MODE=production — the "
            f"context epoch would otherwise fall back to an unkeyed hash, "
            f"which lets a leaked token be correlated back to an account by "
            f"dictionary-hashing candidate ids. Generate a high-entropy "
            f"value, e.g. `python3 -c \"import secrets; "
            f"print(secrets.token_hex(32))\"`, and deploy it identically to "
            f"every replica.")
    return _DEMO_SECRET


def _derived_key(secret: bytes) -> bytes:
    """Fold an arbitrary-length deployment secret down to the exactly-32-byte
    key BLAKE2s accepts (``hashlib.blake2s`` rejects a longer one outright).

    A reshaping step, not a second security boundary: whatever entropy
    ``secret`` carries is what the derived key carries. Using BLAKE2s itself
    (unkeyed, full 32-byte digest) keeps this module's only dependency
    ``hashlib`` and is deterministic, so the same configured secret always
    folds to the same key — required for property 1 (stable across restarts
    and replicas) to survive this step.
    """
    return hashlib.blake2s(secret, digest_size=32).digest()


# Field separator, and a marker for "this field has no value".
#
# EACH FIELD IS ALSO LENGTH-PREFIXED (see :func:`_material`), which is what
# actually makes the encoding injective. A separator alone is only as good as
# the promise that no field contains it — a promise about ids and ISO timestamps
# that happens to hold today and that nothing enforces. With the length in
# front, ("a", "b\x1fc") and ("a\x1fb", "c") produce different material no
# matter what the values are, so the "two different rows can never hash the
# same" claim needs no side conditions at all. The separator stays because it
# costs nothing and keeps the material readable in a debugger.
_SEP = "\x1f"
_NONE = "\x00"
# The EFFECTIVE resolution selects no LIVE Season — either a deliberate
# Program-only selection, or a fallback that found no authorized active
# Season. Both collapse to the SAME well-defined sentinel here (#159 review
# finding 2): unlike the pre-fix material, there is no separate "a selected id
# exists but fails to resolve" state to distinguish, because `season` is
# already the EFFECTIVE object (or None) `ContextService` resolved — a raw,
# possibly-dangling saved id is never hashed directly. Cannot be spelled by a
# real Season, whose fields never start with a NUL.
_SEASON_NOT_SELECTED = "\x00no-season"


def _field(value):
    """One field of the hash material, normalized so the SAME input always
    produces the SAME string.

    ``datetime`` is spelled out via ``isoformat()`` rather than left to
    ``str()``: ``SqlStore`` persists timestamps as ISO text and hydrates them
    back with ``fromisoformat``, so ``isoformat()`` is the spelling that is
    identical on the in-memory, SQLite and PostgreSQL backends — which makes the
    token stable not only within a deployment but across all three, and lets one
    test assert one expected value for all of them.

    ``Enum`` is spelled out for the SAME reason and checked BEFORE the ``str``
    fallback, which is not a formality: ``SeasonStatus`` is a ``(str, Enum)``,
    so it IS a string and would otherwise take that fallback — and what
    ``str()`` returns for a str-mixin enum has changed between Python releases
    ("SeasonStatus.ARCHIVED" vs "archived"). A token whose spelling depends on
    the interpreter would break property 1 across a runtime upgrade, silently
    discarding every in-flight read at exactly the moment nobody is looking for
    it. ``.value`` is the persisted spelling and is the same everywhere.
    """
    if value is None:
        return _NONE
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.value}"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _season_lifecycle_fields(season):
    """The EFFECTIVE Season's lifecycle, as hash-material fields.

    THE DECISION FIRST, and it is asked of ``season_guard.season_is_read_only``
    — the one predicate the write refusal, the candidate-read refusal and the
    hierarchy's published ``read_only`` all already answer to. Recomputing
    "archived" here from ``status`` would be a SECOND definition free to drift
    from that one; the whole point of hashing it is that the epoch moves exactly
    when the refusal's answer could.

    Then the raw ``status`` and ``archived_at``, which is deliberately wider
    than the decision. It costs nothing and it distinguishes archive -> reopen
    -> archive, where the decision alone returns to its starting value. A field
    that moves without the refusal moving can only cause a DISCARD, never a
    serve, so widening here fails in the safe direction.

    ``season`` is the EFFECTIVE object already resolved by the caller (#159
    review finding 2) — never a store lookup performed here. This module does
    not touch a store at all; see the module docstring's WHAT THIS MODULE DOES
    NOT DO.
    """
    if season is None:
        return (_SEASON_NOT_SELECTED, _NONE, _NONE)
    return (_field(season_is_read_only(season)),
            _field(getattr(season, "status", None)),
            _field(getattr(season, "archived_at", None)))


def _material(*fields) -> str:
    """The exact bytes that get hashed: every field length-prefixed, then
    joined. Injective by construction — see the note on ``_SEP``."""
    return _SEP.join(f"{len(f)}:{f}" for f in fields)


def context_epoch(user_id, generation, program, season, league) -> str:
    """The epoch token for the EFFECTIVE resolved context (#159 review
    findings 2+4+5): ``user_id``'s persisted switch ``generation`` plus the
    EFFECTIVE ``(program, season, league)`` tuple
    ``ContextService.resolve_with_league`` would render for this exact
    ``(user_id, role, scope)`` — never the raw saved row, and never a store
    lookup performed here (see the module docstring's WHAT THIS MODULE DOES
    NOT DO). ``program``/``season``/``league`` may each be ``None``; only
    their ``.id`` is hashed (plus, for ``season``, its lifecycle — see
    :func:`_season_lifecycle_fields`).

    A PURE FUNCTION of its five arguments, deliberately: resolving them under
    one consistent snapshot is ``ContextService``'s job
    (``resolve_epoch_state`` / ``_epoch_material_locked`` / ``current_epoch``),
    and incrementing the generation on a write is
    ``ContextService._next_generation_locked``'s. This function holds nothing,
    caches nothing, and consults no clock — call it a million times a second
    or once an hour and it answers the same way for the same input, which is
    the whole reason the mechanism this replaces was retention-based and this
    one is not.

    A falsy ``user_id`` is a well-defined, stable state distinct from every
    real user's — ``_field(None)`` differs from ``_field(<any real id>)`` — so
    callers with no identity to resolve against (see ``ContextService.
    current_epoch``) can pass ``(None, 0, None, None, None)`` without
    colliding with a real, if empty, resolution.

    KEYED with the deployment secret (:func:`epoch_secret`) so recomputing the
    digest — including by enumerating a low-entropy candidate space, #159
    review finding 4's exact attack — requires that secret and not merely the
    material. See the module docstring's NON-DISCLOSURE section.
    """
    material = _material(
        _field(user_id),
        _field(generation),
        _field(getattr(program, "id", None)),
        _field(getattr(season, "id", None)),
        _field(getattr(league, "id", None)),
        *_season_lifecycle_fields(season),
    )
    return hashlib.blake2s(material.encode("utf-8"),
                           key=_derived_key(epoch_secret()),
                           digest_size=_DIGEST_BYTES,
                           person=_PERSON).hexdigest()


def is_epoch_token(value) -> bool:
    """Is ``value`` shaped like a token this module could have issued?

    Shape only — never a claim that it is CURRENT. Used to separate "the caller
    sent no epoch" from "the caller sent something that cannot be one", which
    must take different paths: the first is today's behaviour and the second
    fails closed.
    """
    return isinstance(value, str) and bool(_EPOCH_RE.match(value))


# The three verdicts, as constants rather than bare strings so a caller cannot
# quietly mistype one into a branch that never runs.
EPOCH_ABSENT = "absent"
EPOCH_MATCH = "match"
EPOCH_MISMATCH = "mismatch"


def epoch_verdict(echoed, current) -> str:
    """Compare an ECHOED epoch against the CURRENT one the caller has already
    computed.

    ``current`` is supplied rather than derived here (#159 review findings
    2+3): deriving it requires a role/scope-aware resolution
    (``ContextService.current_epoch``), and ORDERING that derivation against
    a dependent read the caller is about to run is
    ``web/server.py``'s ``Handler._read_under_context_gate``'s job — see the
    module docstring's WHERE IT IS COMPARED section — not a concern this pure
    comparison needs to know about.

    ``EPOCH_ABSENT``   — nothing was echoed. The caller gets exactly the
                         behaviour it gets today; no client is required to
                         participate and none is penalized for not doing so. An
                         empty or whitespace-only value counts as absent, since
                         a client that computed "no epoch yet" and sent the
                         empty string means the same thing as one that sent no
                         header at all.
    ``EPOCH_MISMATCH`` — the selection moved while this request was in
                         transport, OR the value cannot be a token this server
                         issued. Both DISCARD: an ambiguous epoch must never be
                         resolved in favour of serving, and a malformed one can
                         only ever throw away the sender's own request.
    ``EPOCH_MATCH``    — the selection is exactly the one this request was
                         rendered under. Proceed as today, ceiling included.

    The shape check runs BEFORE the equality comparison purely so a garbage
    header costs nothing extra; the verdict is identical either way, because a
    malformed value could never equal a 32-hex digest.
    """
    if echoed is None:
        return EPOCH_ABSENT
    echoed = echoed.strip() if isinstance(echoed, str) else echoed
    if echoed == "" or echoed is None:
        return EPOCH_ABSENT
    if not is_epoch_token(echoed):
        return EPOCH_MISMATCH
    return EPOCH_MATCH if echoed == current else EPOCH_MISMATCH
