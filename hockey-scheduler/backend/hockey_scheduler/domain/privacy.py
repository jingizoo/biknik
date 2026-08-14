"""Sensitive-field categories and the durable sensitive-read record (#124).

The privacy foundation the athlete-identity work (#273) and the future
medical/discipline data (#274) land on. Two things live here, both pure data:

* :class:`SensitiveFieldCategory` — the closed vocabulary of protected field
  groups. Declared NOW, ahead of the fields themselves, so the policy and the
  audit trail exist before the first birthdate is ever stored. The values are
  stable snake_case strings on purpose: they are written into durable audit
  rows, and the #202 route-contract work (RouteSpec sensitive-data
  classification, PR #422's follow-up) can adopt the same strings without a
  mapping layer.

* :class:`DataAccessLog` — one durable row per sensitive read (or refused
  attempt). Modeled on :class:`~.setup_models.SetupAuditLog` but kept as its
  own record type and its own store surface: read auditing must never be
  mixed into the mutation audit trail, where a retention or export decision
  about one would silently apply to the other (#124 blocks 3–4).

STRUCTURALLY VALUE-FREE. ``DataAccessLog`` has no free-form ``detail`` dict
and no field that could carry the protected value — recording *that* a
birthdate or contact destination was read must never copy the birthdate or
the destination into the log (#124: "Never copy the sensitive value itself
into the log"). Keeping the schema closed is the enforcement: a future field
addition has to survive review here, not slip through a dict.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SensitiveFieldCategory(str, Enum):
    """Closed vocabulary of protected field groups (#124).

    Only ``CONTACT_DESTINATION`` has stored data behind it on today's schema
    (the ``ContactDestination`` registry, #60). ``BIRTHDATE`` and
    ``REGISTRATION_NUMBER`` are populated by the athlete-identity slice
    (#273); ``EMERGENCY_CONTACT``, ``MEDICAL_ALERT`` and ``DISCIPLINE_NOTE``
    by their own future slices (#274). They are declared now so policy,
    audit rows and tests use one vocabulary from the first slice on.
    """

    BIRTHDATE = "birthdate"
    REGISTRATION_NUMBER = "registration_number"
    CONTACT_DESTINATION = "contact_destination"
    EMERGENCY_CONTACT = "emergency_contact"
    MEDICAL_ALERT = "medical_alert"
    DISCIPLINE_NOTE = "discipline_note"


# ``DataAccessLog.outcome`` values. A refused attempt MUST still produce a row
# (#124: auditable refusals), and a row that records a refusal must be
# distinguishable from one that records a disclosure — so the outcome is part
# of the record, not inferred.
ACCESS_ALLOWED = "allowed"
ACCESS_DENIED = "denied"


@dataclass
class DataAccessLog:
    """One sensitive read — or refused attempt — of one subject's data (#124).

    ``subject_type``/``subject_id`` name WHOSE data was touched (e.g. the
    ``recipient_ref`` a contact destination belongs to — ``player:<id>``,
    ``official:<id>``, ``scheduler`` — or, later, a Player id for a
    birthdate read). A bulk read that discloses several subjects' values
    writes one row per subject; a read that disclosed nothing (an empty
    registry, or a refusal) writes a single collection-level row with
    ``subject_id="*"`` so every access attempt leaves a trace.

    ``actor_role`` is recorded as a plain string — a ``Role.value``, the
    transitional ``operator_boundary`` principal (see
    ``services.visibility_policy``), or ``"unknown"`` for an unparseable
    claim — never trusted for policy decisions after the fact.

    ``purpose`` is the facade method that performed the read (the facade maps
    1:1 to REST endpoints, so this names the route without hardcoding URL
    strings into durable rows).

    ``request_id`` is the correlation id minted at the facade boundary (none
    exists in the web layer today; the post-#159 server wiring can pass its
    own so one HTTP request's reads share one id).

    There is deliberately NO field for the value that was read — see the
    module docstring.
    """

    id: str
    category: SensitiveFieldCategory
    subject_type: str
    subject_id: str
    purpose: str
    at: datetime
    actor_user_id: Optional[str] = None
    actor_role: Optional[str] = None
    outcome: str = ACCESS_ALLOWED
    request_id: Optional[str] = None
