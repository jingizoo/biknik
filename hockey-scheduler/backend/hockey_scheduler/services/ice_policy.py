"""Effective ice-operations policy resolution (#277).

A Rink runs games under a turnover / warm-up / resurfacing / curfew policy.
Each field is resolved with a simple precedence — the Rink's own value if it
set one, else the Program's value, else a system default — so an operator can
set a Program-wide default once and override it per rink where a building
differs. This is a pure function over the plain :class:`Rink` / :class:`Program`
dataclasses: no store, no I/O, no ``datetime.now``. Enforcement (turnover gaps,
the curfew end-by, and reserved-vs-playable buffers) reads the resolved policy;
it is never written back onto the rink as a concrete value, so changing a
Program default keeps flowing to every rink that inherits it.
"""

from dataclasses import dataclass
from typing import Optional

# System defaults when neither the Rink nor its Program specifies a value.
DEFAULT_TURNOVER_MINUTES = 0
DEFAULT_WARMUP_MINUTES = 0
DEFAULT_RESURFACE_MINUTES = 0
DEFAULT_CURFEW_LOCAL = None  # no curfew

_POLICY_FIELDS = (
    ("turnover_minutes", DEFAULT_TURNOVER_MINUTES),
    ("warmup_minutes", DEFAULT_WARMUP_MINUTES),
    ("resurface_minutes", DEFAULT_RESURFACE_MINUTES),
    ("curfew_local", DEFAULT_CURFEW_LOCAL),
)


@dataclass(frozen=True)
class IcePolicy:
    """A rink's fully-resolved ice-operations policy — every field concrete.

    ``turnover_minutes`` is the minimum gap enforced between two games on the
    rink; ``warmup_minutes`` / ``resurface_minutes`` are the reserved buffers a
    new slot carries so its playable window is ``[start+warmup, end-resurface]``;
    ``curfew_local`` is an ``"HH:MM"`` wall-clock end-by in the Program timezone
    (``None`` = no curfew).
    """
    turnover_minutes: int
    warmup_minutes: int
    resurface_minutes: int
    curfew_local: Optional[str]


def effective_policy(rink, program=None) -> IcePolicy:
    """Resolve ``rink``'s effective ice-operations policy (#277).

    Per field, the first non-``None`` of: the Rink's value, the Program's value,
    the system default. ``program`` may be ``None`` (a rink whose Program is
    unknown/unlinked) — then only the Rink's values and the system defaults
    apply. Neither argument is mutated.
    """
    def resolve(field, default):
        rink_value = getattr(rink, field, None) if rink is not None else None
        if rink_value is not None:
            return rink_value
        program_value = getattr(program, field, None) if program is not None else None
        if program_value is not None:
            return program_value
        return default

    return IcePolicy(**{field: resolve(field, default)
                        for field, default in _POLICY_FIELDS})
