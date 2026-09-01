"""The single v1 setup/reassign boundary adapter (#233 Slice C1b).

The domain and API facade are canonical after the competition-model rename
(``Program``/``League``/``Season``/``Division``/``Team`` with ``program_id`` /
``operator_organization_id`` / competition ``league_id``). The v1 HTTP API
predates that rename, so its ``/api/setup/…`` request bodies and JSON responses
must keep the same JSON keys/shape and values as the pre-C1b contract.

This is the ONE place that reconciles the two vocabularies, used by both the
facade read models (get_demo_overview / get_setup_hierarchy) and the ``web``
write-response dispatch — so the v1 key mapping lives in a single dedicated
layer rather than being duplicated or scattered onto the domain models. It lives
in ``api`` (below ``web``) so the facade can use it without a layer inversion.

Request bodies keep their legacy key names, whose id *values* are unchanged, so
``web/server.py`` passes them straight to the canonical facade — only the
*response* direction needs mapping here.

Mappings (canonical → legacy v1):
  * Program  ``operator_organization_id`` → ``organization_id`` (v1 "league")
  * Season   ``program_id``               → ``league_id``
  * Division ``league_id``                → ``level_id``
  * Team     ``program_id``               → ``league_id``

The grouping ``League`` (was ``Level``) keeps every legacy field name, so its v1
"level" shape needs no mapping. Registration and game results gained an internal
competition ``league_id`` in C1b that the v1 API never exposed; the facade's
``_serialize`` is now purely canonical (it returns that ``league_id``), so this
boundary drops it via ``registration_to_v1`` / ``game_to_v1``. Error envelopes
(``{"error": {...}}``) pass through unchanged.
"""


def _rename(result, mapping):
    """Return ``result`` with any key in ``mapping`` renamed, order-preserving.

    A non-dict or an error envelope is returned untouched so failures keep their
    exact v1 shape.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    return {mapping.get(k, k): v for k, v in result.items()}


def _drop(result, keys):
    """Return ``result`` without any key in ``keys``, order-preserving.

    A non-dict or an error envelope is returned untouched so failures keep their
    exact v1 shape.
    """
    if not isinstance(result, dict) or "error" in result:
        return result
    return {k: v for k, v in result.items() if k not in keys}


def program_to_v1(result):
    """A Program result → the legacy v1 ``league`` shape."""
    return _rename(result, {"operator_organization_id": "organization_id"})


def season_to_v1(result):
    """A Season result → the legacy v1 shape (``program_id`` → ``league_id``).

    #159: Season gained lifecycle fields (``status``/``archived_at``) that the
    frozen v1 contract does not expose — drop them before the rename so the
    legacy key set is unchanged (canonical/v2 consumers still see them)."""
    return _rename(_drop(result, {"status", "archived_at"}),
                   {"program_id": "league_id"})


def division_to_v1(result):
    """A Division result → the legacy v1 shape (``league_id`` → ``level_id``)."""
    return _rename(result, {"league_id": "level_id"})


def team_to_v1(result):
    """A Team result → the legacy v1 shape (``program_id`` → ``league_id``).

    #283: ``Team`` gained a competition ``league_id`` field. Drop it BEFORE the
    rename so the program-derived value isn't clobbered by the (usually None)
    competition league id — the v1 API's ``league_id`` has always meant the
    Program."""
    return _rename(_drop(result, {"league_id"}), {"program_id": "league_id"})


def registration_to_v1(result):
    """A SeasonTeamRegistration result → the legacy v1 shape.

    Drops the internal competition ``league_id`` the v1 API never exposed.
    """
    return _drop(result, {"league_id"})


def game_to_v1(result):
    """A Game result → the legacy v1 shape.

    Drops the internal competition ``league_id`` the v1 API never exposed, and
    the ``game_type`` (#283 Slice D), ``league_season_id`` (#283 Slice E),
    and cancellation-history snapshot (#428) fields the frozen v1 contract
    predates.
    """
    return _drop(result, {
        "league_id", "game_type", "league_season_id",
        "cancelled_ice_slot_id", "cancelled_venue_id",
        "cancelled_venue_name", "cancelled_venue_timezone",
        "cancelled_rink_id", "cancelled_rink_name",
        "cancelled_scheduled_start_time", "cancelled_scheduled_end_time",
        "cancelled_ice_start_time", "cancelled_ice_end_time",
    })
