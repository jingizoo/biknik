"""Canonical sensitive-field visibility policy (#124).

The ONE place that answers "may this role read this protected field
category, and at what fidelity". Mirrors ``subject_scope.py``'s
single-source rule: the facade's sensitive-read gate consults this module,
the later serializer enforcement (post-#159 server wiring) will consult
this module, and tests pin the full matrix — so the decision can never
drift between the surfaces that enforce it.

Three fidelity levels, strictly ordered::

    NONE < SUMMARY < RAW

* ``RAW``     — the stored value itself may be disclosed.
* ``SUMMARY`` — only a derived operational projection may be disclosed
                (e.g. the roster **eligibility summary** a Coach needs —
                "age-eligible for U16: yes" — never the birthdate that
                produced it). The projection itself ships with #273, which
                adds the fields; the POLICY that a Coach tops out at
                SUMMARY ships now.
* ``NONE``    — nothing, not even a derived form.

Decisions this module encodes (owner ruling on #212, 2026-08-09, and #124):

* **Fail-closed.** An unknown role, a missing role, or an unknown category
  resolves to ``NONE``. The tables below enumerate every known role; the
  lookup functions default to ``NONE`` for anything else, so a new role or
  category starts with no access until a reviewed edit here grants it.
* **League Admin is not medical staff.** ``LEAGUE_ADMIN`` holds RAW access
  to birthdate/registration/contact categories but ``NONE`` — not even
  SUMMARY — for ``MEDICAL_ALERT``/``DISCIPLINE_NOTE``. Defining the minimum
  authorized staff roles (and their recorded purpose) for those categories
  is #274's job; until then nobody has them.
* **Ordinary Coaches get eligibility summaries, never raw values.**
  ``COACH`` is ``SUMMARY`` for exactly the two athlete-eligibility
  categories and ``NONE`` elsewhere.
* **Arena Managers operate the delivery queue** (MANAGE_SCHEDULE — the same
  permission the operator-only ``/api/notifications/contacts`` route
  requires today, ``web/authz.py``), so they hold RAW
  ``CONTACT_DESTINATION`` and nothing else.
* **Viewer/Player/Guardian/Official/public: nothing.** A person's access to
  their OWN data (or a guardian's to a linked junior's) is subject-scoped
  self-service — #275's consent/provenance surface — not a role-wide grant,
  and deliberately absent from this role-keyed table.

PRINCIPALS (#426 review finding 1 — retired the transitional
``OPERATOR_BOUNDARY`` shape). Every HTTP-originated call now propagates the
real, session-resolved :class:`Role` (``web/server.py``'s ``_resolve_role()``
never returns a live request with ``role=None`` — a missing/invalid session
is an ``err``, resolved to an HTTP status BEFORE any facade call, never to a
silent default-allow principal). Two non-:class:`Role` principals remain,
both fail-closed by construction:

* :data:`NO_PRINCIPAL` — the label recorded when a caller passes no role
  information at all (or an unparseable one). Resolves to ``NONE`` on every
  category, same as any other unrecognised principal — there is no more
  "missing principal defaults to RAW on CONTACT_DESTINATION" carve-out. Kept
  distinct from ``"unknown"`` (an unparseable string) purely so an audit row
  reads WHY nothing was resolved, never to grant anything.
* :data:`SYSTEM_PRINCIPAL` — a real, explicit, NON-Role sentinel for
  legitimate background/worker code that is not acting on behalf of any
  signed-in user (the notification delivery worker resolving a stored
  contact destination to actually send a message, #426 review finding 2). A
  Python object identity, never a string — so no HTTP-supplied value (a
  header, a body field, a role claim) can ever equal it; only trusted
  first-party call sites that import this module and pass the sentinel
  object itself can invoke it. Granted RAW on exactly
  :data:`SYSTEM_ATTESTED_CATEGORIES`, checked in :func:`access_level` the
  same fail-closed way every other principal is.
"""

import hashlib
import re
import uuid
from enum import IntEnum

from ..domain import Role
from ..domain.privacy import SensitiveFieldCategory as _C

#: Recorded when a caller supplied no role information at all (``None``) —
#: fails closed exactly like an unparseable claim; see the module docstring's
#: PRINCIPALS section. NOT a :class:`Role` and never looked up in `_POLICY`.
NO_PRINCIPAL = "no_principal"


class _SystemPrincipal:
    """Sentinel type for :data:`SYSTEM_PRINCIPAL` — identity-only, never
    stringified for comparison, so nothing caller-supplied can ever equal
    it. A single module-level instance exists; see the module docstring."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "SYSTEM_PRINCIPAL"


#: The one instance. Trusted, first-party, non-HTTP callers only (e.g.
#: ``services/delivery.py``'s worker) — see the module docstring's
#: PRINCIPALS section.
SYSTEM_PRINCIPAL = _SystemPrincipal()

#: Categories :data:`SYSTEM_PRINCIPAL` may read RAW. Deliberately narrow and
#: separately named from :func:`raw_categories` (which is keyed by
#: :class:`Role`) — a future sensitive category must extend this set on
#: purpose, in review, not inherit it implicitly.
SYSTEM_ATTESTED_CATEGORIES = frozenset({_C.CONTACT_DESTINATION})


class AccessLevel(IntEnum):
    """Disclosure fidelity, ordered so ``>=`` means "at least"."""

    NONE = 0
    SUMMARY = 1
    RAW = 2


# The full role × category matrix. EVERY known role appears, categories a
# role holds nothing of are simply absent from its mapping (lookup defaults
# to NONE) — but the two deliberate "and specifically not medical/discipline"
# rulings are spelled out as explicit NONE entries so a reader (and the
# matrix test) sees the decision, not an omission.
_POLICY = {
    Role.LEAGUE_ADMIN: {
        _C.BIRTHDATE: AccessLevel.RAW,
        _C.REGISTRATION_NUMBER: AccessLevel.RAW,
        _C.CONTACT_DESTINATION: AccessLevel.RAW,
        _C.EMERGENCY_CONTACT: AccessLevel.RAW,
        # NOT auto-granted (#124): being the League Admin is not a medical
        # or disciplinary need-to-know. #274 defines who is.
        _C.MEDICAL_ALERT: AccessLevel.NONE,
        _C.DISCIPLINE_NOTE: AccessLevel.NONE,
    },
    Role.ARENA_MANAGER: {
        _C.CONTACT_DESTINATION: AccessLevel.RAW,
    },
    Role.COACH: {
        # The operational eligibility summary needed to roster a player —
        # never the raw birthdate/registration number (#124).
        _C.BIRTHDATE: AccessLevel.SUMMARY,
        _C.REGISTRATION_NUMBER: AccessLevel.SUMMARY,
    },
    Role.PLAYER: {},
    Role.GUARDIAN: {},
    Role.OFFICIAL: {},
    Role.VIEWER: {},
}


def access_level(role, category) -> AccessLevel:
    """The fidelity ``role`` may read ``category`` at. Fail-closed: any
    role or category not explicitly granted — including ``None``, an
    unknown role string, ``NO_PRINCIPAL``, or a brand-new category — is
    ``NONE``.

    :data:`SYSTEM_PRINCIPAL` is the one non-:class:`Role` key with any
    grant at all (:data:`SYSTEM_ATTESTED_CATEGORIES`, RAW) — checked here
    by identity (``is``), never by value, so nothing derived from caller
    input can ever match it (see the module docstring's PRINCIPALS
    section)."""
    if role is SYSTEM_PRINCIPAL:
        return (AccessLevel.RAW if category in SYSTEM_ATTESTED_CATEGORIES
                else AccessLevel.NONE)
    return _POLICY.get(role, {}).get(category, AccessLevel.NONE)


def may_read_raw(role, category) -> bool:
    """True only when ``role`` may see the stored value itself."""
    return access_level(role, category) >= AccessLevel.RAW


def may_read_summary(role, category) -> bool:
    """True when ``role`` may see at least a derived summary projection."""
    return access_level(role, category) >= AccessLevel.SUMMARY


def raw_categories(role) -> frozenset:
    """Every category ``role`` holds RAW access to (for serializers that
    shape whole payloads rather than asking field by field)."""
    return frozenset(c for c, lvl in _POLICY.get(role, {}).items()
                     if lvl >= AccessLevel.RAW)


# -- correlation-id / subject-id sanitization (#426 review finding 3) -------
#
# Shared here (not on ApiService) so every writer of a DataAccessLog row —
# the HTTP-facing facade AND trusted non-HTTP callers like the delivery
# worker (services/delivery.py), which cannot import api/service.py without
# a circular import — sanitizes caller-controlled metadata through the SAME
# one function, never two copies that could drift.

#: Bounded, opaque correlation-id shape. Anything that doesn't match — an
#: email, a token, a newline, anything oversized — is never persisted
#: verbatim; see safe_request_id.
_REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9_-]{1,80}$")

#: Bounded, opaque subject-id shape. Ordinary recipient refs
#: (player:<id>, official:<id>, scheduler, *, ...) are already this shape
#: — ``*`` included: the collection-level "nothing disclosed" sentinel every
#: refusal and every empty-registry read already uses is a deliberate,
#: fixed, one-character convention (never raw caller input), not something
#: this boundary needs to protect against; see canonical_subject_id.
_SUBJECT_ID_RE = re.compile(r"^[A-Za-z0-9_:.*-]{1,200}$")


def mint_request_id() -> str:
    """A fresh, trusted correlation id for one sensitive-read call."""
    return f"req_{uuid.uuid4().hex}"


def safe_request_id(candidate) -> str:
    """A trusted, bounded, opaque correlation id.

    ``candidate`` is honoured ONLY when it already looks like an opaque id
    this module itself would mint — never because the caller asked nicely.
    A value that doesn't match (an email or token planted AS a request_id,
    a newline, anything oversized) is discarded and a fresh one minted
    instead, exactly as if no id had been supplied at all — so the
    "value-free" log can never be made to persist a protected value riding
    along disguised as a correlation id.
    """
    if isinstance(candidate, str) and _REQUEST_ID_RE.match(candidate):
        return candidate
    return mint_request_id()


def canonical_subject_id(raw) -> str:
    """A bounded, opaque subject identifier.

    Ordinary recipient refs (``player:<id>``, ``official:<id>``,
    ``scheduler``, ``*``, a bare team/guardian id, ...) already match the
    safe shape and pass through unchanged. Anything that doesn't — an email
    or token planted AS a recipient_ref, a newline, anything oversized — is
    NEVER persisted verbatim: it is replaced with a stable, deterministic,
    collision-resistant pseudonym derived from it, so the row still exists
    and still groups repeat reads of the SAME (unsafe) subject together,
    without ever copying the original string's content into the
    "structurally value-free" log.
    """
    if isinstance(raw, str) and _SUBJECT_ID_RE.match(raw):
        return raw
    text = "" if raw is None else str(raw)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return f"opaque:{digest[:24]}"
