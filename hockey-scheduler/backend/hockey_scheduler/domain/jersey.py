"""Jersey-number rules (#269).

A single source of truth for the two jersey invariants, shared by the service
layer (typed ``int`` values), the CSV import validators (string cell values),
and the migration preflight — so no caller can drift on the allowed range.

* Range: an integer ``1..98`` inclusive, or ``None`` (no number assigned).
  A JSON boolean is NOT an integer here even though ``bool`` subclasses ``int``.
* Active-team uniqueness is enforced in the service layer + a partial DB unique
  index; it is not expressible as a pure per-value rule, so it lives elsewhere.
"""

from typing import Optional

MIN_JERSEY_NUMBER = 1
MAX_JERSEY_NUMBER = 98


def jersey_number_error(value) -> Optional[str]:
    """Return a stable reason string if ``value`` is not a valid jersey number.

    ``None`` is valid (no number) → returns ``None``. A ``bool`` is rejected
    (``True``/``False`` are not jersey numbers even though ``bool`` is an
    ``int`` subclass). A non-integer is ``not_an_integer``; an integer outside
    ``MIN_JERSEY_NUMBER..MAX_JERSEY_NUMBER`` is ``out_of_range``. A valid value
    returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        return "not_an_integer"
    if value < MIN_JERSEY_NUMBER or value > MAX_JERSEY_NUMBER:
        return "out_of_range"
    return None


def parse_jersey_cell(value) -> tuple:
    """Parse a spreadsheet cell into ``(jersey_number, error_reason)`` (#269).

    A blank/absent cell is a valid absent number → ``(None, None)``. A cell that
    isn't a base-10 integer is ``(None, "not_an_integer")``; an in-form integer
    outside the allowed range is ``(value, "out_of_range")``. A clean value is
    ``(int, None)``. Cells arrive as strings from CSV, so this never sees a
    Python ``bool``; range/formatting is the only concern here.
    """
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None, "not_an_integer"
    reason = jersey_number_error(number)
    return number, reason
