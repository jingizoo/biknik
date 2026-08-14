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
"""

from enum import IntEnum

from ..domain import Role
from ..domain.privacy import SensitiveFieldCategory as _C

#: Transitional principal label (#124) for facade calls that arrive through a
#: server route which already enforced the operator gate (``_operator_only`` /
#: ``authorize()`` in ``web/server.py``) but does not yet propagate the acting
#: account into the facade. Those call sites keep working — and keep being
#: AUDITED, attributed to this marker — until the post-#159 wiring passes the
#: real actor through. NOT a :class:`Role`; never grants anything outside
#: :func:`boundary_attested_categories`.
OPERATOR_BOUNDARY = "operator_boundary"


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
    role or category not explicitly granted in ``_POLICY`` — including
    ``None``, an unknown role string, or a brand-new category — is
    ``NONE``."""
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


def boundary_attested_categories() -> frozenset:
    """The categories the transitional :data:`OPERATOR_BOUNDARY` principal
    may read RAW.

    Exactly the sensitive surface whose operator-only server gate exists
    today: the ContactDestination registry (``/api/notifications/contacts``,
    gated by ``_operator_only`` → MANAGE_SCHEDULE before the facade is
    called). Kept to this ONE category on purpose — a future sensitive
    surface must arrive with real actor propagation, not ride the legacy
    no-actor call shape; the pinning test fails if this set ever grows.
    """
    return frozenset({_C.CONTACT_DESTINATION})
