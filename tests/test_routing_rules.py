"""Tests for the EMEA routing rules engine.

Each test maps to one bullet from the Transfer Restrictions doc:
Amsterdam (CSNL office / MRIS-NL office / data-centre server),
Switzerland (office vs server), Saudi (server vs furniture), and the
KL tax-block.
"""

from __future__ import annotations

import pytest

from ofam_asset_xfer.routing_rules import (
    RoutingDecision,
    RoutingRule,
    RoutingRulesResolver,
)


# Stand-in Oracle FA category IDs for readability.
CAT_OFFICE_TECH = 82001
CAT_SERVER = 82010
CAT_FURNITURE = 82020
CAT_LEASEHOLD = 82030


def _emea_resolver() -> RoutingRulesResolver:
    """A complete EMEA rule set covering every example from the spec."""
    return RoutingRulesResolver.from_config(
        {
            "blocked_locations": {
                "LC000099": "KL — tax confirmation pending; review 2026-09-01"
            },
            "rules": [
                # Amsterdam: CSNL office tech
                {
                    "name": "AMS_CSNL_office",
                    "location_in": ["LC000030"],
                    "category_in": [CAT_OFFICE_TECH],
                    "target_book": "CSNL CORP BOOK",
                },
                # Amsterdam: CSNL leasehold (identified by cost-centre)
                {
                    "name": "AMS_CSNL_leasehold",
                    "cost_centre_in": ["30755"],
                    "category_in": [CAT_LEASEHOLD],
                    "target_book": "CSNL CORP BOOK",
                },
                # Amsterdam: MRIS-NL office tech
                {
                    "name": "AMS_MRIS_office",
                    "location_in": ["LC000031"],
                    "category_in": [CAT_OFFICE_TECH],
                    "target_book": "MRIS-NL CORP BOOK",
                },
                # EMEA-wide: data-centre servers / trading hardware -> CETS
                {
                    "name": "EMEA_servers_to_CETS",
                    "location_starts_with": ["LC0001", "LC0002"],
                    "category_in": [CAT_SERVER],
                    "target_book": "CETS CORP BOOK",
                },
                # Switzerland: office tech routed by location prefix
                {
                    "name": "CH_office",
                    "location_starts_with": ["LC0050"],
                    "category_in": [CAT_OFFICE_TECH],
                    "target_book": "CSZH CORP BOOK",
                },
                # Saudi: furniture / leasehold
                {
                    "name": "SA_furniture_leasehold",
                    "location_starts_with": ["LC0080"],
                    "category_in": [CAT_FURNITURE, CAT_LEASEHOLD],
                    "target_book": "CEDIFC CORP BOOK",
                },
            ],
        }
    )


# --- spec example 1: Amsterdam -------------------------------------------


def test_amsterdam_csnl_office_tech_routes_to_csnl() -> None:
    decision = _emea_resolver().resolve(
        location="LC000030", category_id=CAT_OFFICE_TECH
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CSNL CORP BOOK"
    assert decision.rule_name == "AMS_CSNL_office"


def test_amsterdam_mris_office_tech_routes_to_mris() -> None:
    decision = _emea_resolver().resolve(
        location="LC000031", category_id=CAT_OFFICE_TECH
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "MRIS-NL CORP BOOK"


def test_amsterdam_data_centre_server_routes_to_cets() -> None:
    decision = _emea_resolver().resolve(
        location="LC000123", category_id=CAT_SERVER
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CETS CORP BOOK"
    assert decision.rule_name == "EMEA_servers_to_CETS"


def test_csnl_leasehold_identified_by_cost_centre() -> None:
    """Spec: 'leasehold identified by cost centre' — location ignored."""
    decision = _emea_resolver().resolve(
        location="LC000999",
        category_id=CAT_LEASEHOLD,
        cost_centre="30755",
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CSNL CORP BOOK"
    assert decision.rule_name == "AMS_CSNL_leasehold"


# --- spec example 2: Switzerland -----------------------------------------


def test_switzerland_office_routes_to_cszh() -> None:
    decision = _emea_resolver().resolve(
        location="LC005010", category_id=CAT_OFFICE_TECH
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CSZH CORP BOOK"


def test_switzerland_server_routes_to_cets() -> None:
    """Same country, server category — falls into the EMEA servers rule."""
    decision = _emea_resolver().resolve(
        location="LC000150", category_id=CAT_SERVER
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CETS CORP BOOK"


# --- spec example 3: Saudi (future) --------------------------------------


def test_saudi_server_routes_to_cets() -> None:
    decision = _emea_resolver().resolve(
        location="LC000201", category_id=CAT_SERVER
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CETS CORP BOOK"


def test_saudi_furniture_routes_to_cedifc() -> None:
    decision = _emea_resolver().resolve(
        location="LC008010", category_id=CAT_FURNITURE
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CEDIFC CORP BOOK"


def test_saudi_leasehold_routes_to_cedifc() -> None:
    """Saudi leasehold rule covers both furniture *and* leasehold categories."""
    decision = _emea_resolver().resolve(
        location="LC008020", category_id=CAT_LEASEHOLD
    )
    assert decision.kind == "route_to"
    assert decision.target_book == "CEDIFC CORP BOOK"


# --- block list ----------------------------------------------------------


def test_kl_blocked_location_short_circuits() -> None:
    decision = _emea_resolver().resolve(
        location="LC000099", category_id=CAT_OFFICE_TECH
    )
    assert decision.kind == "blocked"
    assert "KL" in (decision.reason or "")


def test_blocked_check_is_case_insensitive() -> None:
    decision = _emea_resolver().resolve(
        location="lc000099", category_id=CAT_OFFICE_TECH
    )
    assert decision.kind == "blocked"


# --- no-match fallback ---------------------------------------------------


def test_unknown_location_falls_back_to_no_match() -> None:
    """No silent default — finance has to add a rule, not get bad data."""
    decision = _emea_resolver().resolve(
        location="LC999999", category_id=99999
    )
    assert decision.kind == "no_match"
    assert "no routing rule matched" in (decision.reason or "")


def test_missing_category_does_not_match_category_constrained_rule() -> None:
    decision = _emea_resolver().resolve(location="LC000030", category_id=None)
    assert decision.kind == "no_match"


# --- RoutingRule unit checks ---------------------------------------------


def test_rule_with_no_predicates_never_matches() -> None:
    """Wildcard rules are almost always a config bug — reject by design."""
    rule = RoutingRule(name="bug", target_book="DEFAULT")
    assert not rule.matches(location="LC000030", category_id=82001, cost_centre="30755")


def test_rule_predicates_are_AND_not_OR() -> None:
    rule = RoutingRule(
        name="ams_csnl",
        target_book="CSNL CORP BOOK",
        location_in=("LC000030",),
        category_in=(CAT_OFFICE_TECH,),
    )
    # Right location, wrong category → no match.
    assert not rule.matches(
        location="LC000030", category_id=CAT_SERVER, cost_centre=None
    )
    # Right category, wrong location → no match.
    assert not rule.matches(
        location="LC000031", category_id=CAT_OFFICE_TECH, cost_centre=None
    )
    # Both right → match.
    assert rule.matches(
        location="LC000030", category_id=CAT_OFFICE_TECH, cost_centre=None
    )


def test_first_match_wins_in_rule_order() -> None:
    """Two rules that both match — the earlier one wins."""
    resolver = RoutingRulesResolver.from_config(
        {
            "rules": [
                {
                    "name": "specific",
                    "location_in": ["LC000030"],
                    "category_in": [CAT_OFFICE_TECH],
                    "target_book": "BOOK_A",
                },
                {
                    "name": "broad",
                    "location_starts_with": ["LC0000"],
                    "target_book": "BOOK_B",
                },
            ]
        }
    )
    decision = resolver.resolve(location="LC000030", category_id=CAT_OFFICE_TECH)
    assert decision.target_book == "BOOK_A"
    assert decision.rule_name == "specific"


# --- from_config / from_dict validation ----------------------------------


def test_from_dict_requires_name_and_target_book() -> None:
    with pytest.raises(ValueError, match="name"):
        RoutingRule.from_dict({"location_in": ["LC000030"]})


def test_from_config_with_empty_block_returns_empty_resolver() -> None:
    resolver = RoutingRulesResolver.from_config({})
    assert resolver.rules == ()
    assert resolver.blocked_locations == {}
    assert resolver.resolve(location="LC000030", category_id=82001).kind == "no_match"
