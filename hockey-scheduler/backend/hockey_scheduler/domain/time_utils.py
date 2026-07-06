"""Small time-interval helpers shared by scheduling and import validation."""


def intervals_overlap(start_a, end_a, start_b, end_b) -> bool:
    """True if half-open intervals ``[start_a, end_a)`` and ``[start_b,
    end_b)`` overlap. Back-to-back intervals (one starts exactly when the
    other ends) do not count as overlapping."""
    return start_a < end_b and end_a > start_b
