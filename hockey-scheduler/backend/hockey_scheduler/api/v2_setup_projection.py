"""Canonical v2 setup/reassign response projections (#233 Slice C2).

The v2 ``/api/v2/setup/…`` surface is canonical: it takes canonical body keys and
returns the facade's canonical ``_serialize`` output directly, WITHOUT the v1
boundary adapter. The one exception is the temporary Venue→Program ``league_id``
column (a v1-era link removed in Slice E): the canonical Venue is
Organization-owned only, so the v2 surface must strip that legacy field from
every Venue response.

That projection lives here — a v2-only module — rather than in
``v1_setup_adapter`` so the two API boundaries stay separate: v2 does not depend
on the v1 adapter.
"""


def _drop(result, keys):
    """Return ``result`` without any key in ``keys``, order-preserving.

    A non-dict or an error envelope is returned untouched so failures keep their
    exact structured shape.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    return {k: v for k, v in result.items() if k not in keys}


def venue_to_v2(result):
    """A Venue result → the canonical v2 shape.

    Drops the legacy ``league_id`` (the temporary Venue→Program link) the v2
    surface never exposes, from every v2 Venue response (create, reassign,
    delete). v1 Venue responses keep ``league_id`` unchanged.
    """
    return _drop(result, {"league_id"})
