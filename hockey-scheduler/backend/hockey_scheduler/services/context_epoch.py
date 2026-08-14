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
    eviction and no configuration. The epoch is DERIVED, on demand, from the
    row that ``ActiveContext`` already persists. An arrival delayed by an hour
    is compared exactly as correctly as one delayed by a millisecond, because
    the comparison reads the same persisted row either way. The rejected
    failure mode is UNCONSTRUCTIBLE, not merely unlikely.

THE MECHANISM, in three sentences. The server derives an opaque token from the
persisted ``ActiveContext`` row and hands it out wherever the client already
learns its context (``GET /api/context``, ``GET /api/context/options``, and the
``POST /api/context`` response). Each context-scoped GET echoes, in
``X-Context-Epoch``, the token it was RENDERED UNDER. On arrival the server
compares that echo with the token derived from the row as it stands NOW: equal
means the selection has not moved and the request proceeds exactly as it does
today, ceiling included; unequal means the tuple moved while the request was in
transport, and it is answered ``204 No Content`` before the ceiling is ever
evaluated.

FOUR PROPERTIES, each of which this module has to earn:

  1. STABLE FOR A GIVEN PERSISTED ROW. The same row hashes to the same token
     every time, in every process, across restarts — the material is the row,
     and nothing else. Deliberately NOT a per-process counter or a random
     nonce: either would make every restart invalidate every outstanding read,
     and would make the token mean "which process answered" rather than "which
     selection".
  2. DIFFERENT AFTER ANY SWITCH, INCLUDING A SWITCH BACK TO THE SAME TUPLE.
     ``ContextService.set_with_league`` writes ``updated_at=self.clock()`` on
     every commit, so A -> B -> A moves the timestamp twice and therefore moves
     the token twice, even though the tuple ends where it began. A token
     derived from the tuple ALONE would have silently readmitted a read from
     before the round trip.
  3. OPAQUE AND NON-IDENTIFYING. The material is HASHED, never concatenated or
     encoded, so no user id, Program id, Season id, League id or timestamp can
     be read out of a token. See NON-DISCLOSURE below for the honest limit of
     that claim.
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
directions. ``Handler._read_under_context_gate`` compares AFTER the gate's
shared hold is taken and BEFORE the service call:
  * AFTER the hold, so a read that arrives mid-quiesce has already waited out
    the switch that registered before it, and therefore reads the row as it
    stands after that switch committed — never a half-applied one. Arrival
    ordering for reads ALREADY inside the server is untouched: those hold the
    gate, the switch waits for them, and their echo still matches.
  * BEFORE the service call, so a discarded read never reaches
    ``api/service.py`` and the exact-Season ceiling is never evaluated for it.
    That is what lets the answer disclose nothing in EITHER direction — a
    discard looks identical whether the named Season is a sibling of the
    selection, is archived, or never existed.

THE ONE HONEST DEGRADATION. Two switches that land inside the same clock tick
would write the same ``updated_at`` and therefore the same token, and a read
rendered under the first would not be discarded. ``datetime.now()`` resolves to
a microsecond and two sequential HTTP commits cannot share one, but the failure
is worth naming because of what it costs: such a read is judged by the ceiling,
which is precisely the pre-#159 behaviour — a missed improvement, never a wrong
answer, and never a disclosure. There is no interleaving in which a coarse
clock causes data to be SERVED that would otherwise be refused.

PER PROCESS? NO — and that is the difference from ``context_gate.py``. The gate
is honestly per-process because it holds locks. This holds nothing: the epoch is
a pure function of a row in the shared database, so a token issued by replica A
compares correctly on replica B, and a restart mid-flight changes nothing. If
this application is ever run multi-replica, this half needs no re-expression.

WHY THE SELECTED SEASON'S LIFECYCLE IS PART OF THE MATERIAL, and not only the
``ActiveContext`` row. The token above answers "has the operator's SELECTION
moved". That is not the whole question the scoped reads are judged by, and the
gap was measured rather than reasoned about:

    the Setup hierarchy answers, reporting `read_only: false` for the selected
      Season, and the client decides — correctly, on that snapshot — to fetch
      its grant candidates
    the Season is ARCHIVED (a second tab, another operator, this operator)
    the candidate GET arrives; `season_is_read_only` is now true and the
      unchanged exact-Season ceiling answers its deliberate generic 404

    MEASURED: an archive writes ``Season.status``/``archived_at`` and touches
    NO ``ActiveContext`` row, so a token derived from that row alone was
    BYTE-IDENTICAL before and after the archive. The epoch could not see the
    transition, so it could not discard the read, and the 404 survived the
    mechanism built to remove it.

So the material also carries the SELECTED Season's lifecycle, asked through
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
    expected it to. The tuple axis differs because ``updated_at`` moves on
    every commit — there the two ends only LOOK alike.
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
# property 1 above requires that the same row hash identically after a restart.
# EXACTLY 8 BYTES because blake2s rejects anything longer, and the trailing "2"
# is a format version — if the material below ever changes shape, bump it rather
# than reusing this one, so old and new tokens cannot be confused for each other
# during a rollout.
#
# BUMPED FROM "1" TO "2" when the selected Season's lifecycle joined the
# material. That is the shape change the rule above is written for, and the
# rollout it protects is real: a browser holding a "1" token would otherwise
# have it compared against a "2" token derived from an unchanged row, and the
# two must be UNEQUAL rather than coincidentally equal. Unequal is the safe
# answer — those in-flight reads discard once and are re-issued under the new
# token — whereas a reused personalization would make the old and new formats
# collide only for rows whose Season happens to hash to the same suffix, i.e.
# it would fail in a way nobody could predict.
_PERSON = b"hsctxep2"

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
# A row that does not exist at all (a user who has never made a selection) is
# still an epoch, and a well-defined one: it is the state the first switch will
# move AWAY from, so a read rendered before that first switch must be discarded
# by it. Distinct from every real row because a real row always carries an id.
_ABSENT_ROW = "\x00absent-row"
# The two ways a row can select no LIVE Season, kept apart from each other and
# from every real lifecycle. "Program-only" is a legitimate, common selection;
# "the selected Season id no longer resolves" is a deleted Season, and a read
# rendered under it must not be admitted just because the lookup came back
# empty. Neither can be spelled by a real Season, whose fields never start with
# a NUL.
_SEASON_NOT_SELECTED = "\x00no-season"
_SEASON_UNRESOLVABLE = "\x00season-gone"


def _field(value):
    """One field of the hash material, normalized so the SAME persisted row
    always produces the SAME string.

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


def _selected_season_lifecycle(store, season_id):
    """The SELECTED Season's lifecycle, as hash-material fields.

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

    ``store.get_season`` is called unguarded on purpose. Every store in this
    repository implements it, and a ``getattr(store, "get_season", None)``
    fallback would let a store that cannot answer produce a confident-looking
    token that is blind to the exact transition this function exists to see —
    the failure would be silent, which is the one thing it must not be.
    """
    if not season_id:
        return (_SEASON_NOT_SELECTED, _NONE, _NONE)
    season = store.get_season(season_id)
    if season is None:
        return (_SEASON_UNRESOLVABLE, _NONE, _NONE)
    return (_field(season_is_read_only(season)),
            _field(getattr(season, "status", None)),
            _field(getattr(season, "archived_at", None)))


def _material(*fields) -> str:
    """The exact bytes that get hashed: every field length-prefixed, then
    joined. Injective by construction — see the note on ``_SEP``."""
    return _SEP.join(f"{len(f)}:{f}" for f in fields)


def context_epoch(store, user_id) -> str:
    """The epoch token for ``user_id``'s CURRENTLY PERSISTED context row.

    Reads the row and hashes it. Holds nothing, caches nothing, and consults no
    clock — call it a million times a second or once an hour and it answers from
    the same place, which is the whole reason this replaced a retention-based
    design.

    ``store.get_active_context`` is the PERSISTED row, deliberately, not
    ``ApiService.get_active_context``'s scope-filtered *resolution* of it. The
    epoch is a change detector for the operator's saved selection; a resolution
    can differ between two callers of the same row (role, scope, an archived
    parent) and would make the token depend on who asked, which is exactly what
    an epoch must not do.

    ...AND THE SELECTED SEASON'S LIFECYCLE, because the row alone cannot see an
    archive: archiving writes the SEASON, never the ``ActiveContext``, so a
    token derived from the row was measured byte-identical either side of the
    transition that flips the candidate read from 200 to its deliberate 404.
    See the module docstring for the interleaving that made it visible.
    """
    row = store.get_active_context(user_id) if user_id else None
    if row is None:
        # No row means no selection, hence no selected Season to have a
        # lifecycle. The Season fields are omitted rather than filled with
        # sentinels: this branch's material is already distinct from every real
        # row's (no real row hashes ``_ABSENT_ROW`` first), and adding three
        # constant fields to it would say nothing.
        material = _material(_ABSENT_ROW, _field(user_id))
    else:
        material = _material(
            _field(getattr(row, "id", None)),
            _field(getattr(row, "program_id", None)),
            _field(getattr(row, "season_id", None)),
            _field(getattr(row, "league_id", None)),
            _field(getattr(row, "updated_at", None)),
            *_selected_season_lifecycle(
                store, getattr(row, "season_id", None)),
        )
    # KEYED (#159 review finding 4): the digest is now a MAC, not a plain
    # hash, so recovering it — including by enumerating a small candidate
    # space of ids, the finding's exact PoC — requires the deployment secret,
    # not merely the material. See `epoch_secret` and the module docstring's
    # NON-DISCLOSURE section.
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


def epoch_verdict(store, user_id, echoed) -> str:
    """Compare an echoed epoch against the current persisted one.

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

    The shape check runs BEFORE the store read purely so a garbage header costs
    no query; the verdict is identical either way, because a malformed value
    could never equal a 32-hex digest.
    """
    if echoed is None:
        return EPOCH_ABSENT
    echoed = echoed.strip() if isinstance(echoed, str) else echoed
    if echoed == "" or echoed is None:
        return EPOCH_ABSENT
    if not is_epoch_token(echoed):
        return EPOCH_MISMATCH
    return (EPOCH_MATCH if echoed == context_epoch(store, user_id)
            else EPOCH_MISMATCH)
