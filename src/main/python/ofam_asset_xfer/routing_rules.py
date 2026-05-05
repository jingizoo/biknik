"""EMEA routing-rules resolver for cross-book asset transfer.

Decides which Oracle FA book a given asset should be transferred to,
based on a priority-ordered list of rules over three CMDB-side
dimensions: the FA Citadel internal location code (e.g. ``LC000030``),
the Oracle FA category ID (numeric), and the cost-centre segment.

The Transfer Restrictions doc owns the rule content; this module is
just the matcher.  It also handles the tax-blocked-jurisdiction list
(``blocked_locations``) so an LC code that's on hold short-circuits
out of the cycle with a clean ``skip`` audit event instead of routing
to a default book.

Usage::

    resolver = RoutingRulesResolver.from_config(cfg)
    decision = resolver.resolve(
        location="LC000030",
        category_id=82001,
        cost_centre="30755",
    )
    if decision.kind == "blocked":
        ...skip the asset...
    elif decision.kind == "no_match":
        ...raise as exception so finance can fix the rule set...
    else:
        target_book = decision.target_book
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingRule:
    """One routing rule.  Predicates AND together; unset = unconstrained.

    ``location_in`` matches the LC code exactly; ``location_starts_with``
    matches a prefix (useful for grouping all ``CH-DC*`` data centres).
    ``category_in`` matches the Oracle FA numeric category ID, so a
    leasehold asset that lives under category 82010 routes the same
    way regardless of its physical location code.  ``cost_centre_in``
    matches the cost-centre segment string (e.g. ``"30755"``) — the
    EMEA spec uses this for "leasehold identified by cost centre".
    """

    name: str
    target_book: str
    location_in: tuple[str, ...] = ()
    location_starts_with: tuple[str, ...] = ()
    category_in: tuple[int, ...] = ()
    cost_centre_in: tuple[str, ...] = ()

    def matches(
        self,
        *,
        location: str | None,
        category_id: int | None,
        cost_centre: str | None,
    ) -> bool:
        """True iff every configured predicate matches the asset."""
        if self.location_in and (location or "") not in self.location_in:
            return False
        if self.location_starts_with:
            loc = location or ""
            if not any(loc.startswith(p) for p in self.location_starts_with):
                return False
        if self.category_in and (category_id is None or category_id not in self.category_in):
            return False
        if self.cost_centre_in and (cost_centre or "") not in self.cost_centre_in:
            return False
        # No predicates set at all → reject; a wildcard rule is almost
        # certainly a config bug, and silently routing every asset to one
        # book would be a finance disaster.
        if not (
            self.location_in
            or self.location_starts_with
            or self.category_in
            or self.cost_centre_in
        ):
            return False
        return True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoutingRule":
        """Build a RoutingRule from a JSON-friendly dict."""
        if "name" not in d or "target_book" not in d:
            raise ValueError(
                f"RoutingRule needs 'name' and 'target_book': {d}"
            )
        return cls(
            name=str(d["name"]),
            target_book=str(d["target_book"]),
            location_in=tuple(d.get("location_in") or ()),
            location_starts_with=tuple(d.get("location_starts_with") or ()),
            category_in=tuple(int(x) for x in (d.get("category_in") or ())),
            cost_centre_in=tuple(str(x) for x in (d.get("cost_centre_in") or ())),
        )


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of a single ``resolve()`` call."""

    kind: Literal["route_to", "blocked", "no_match"]
    target_book: str | None = None
    rule_name: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RoutingRulesResolver:
    """Walks rules in priority order; first match wins.

    Block list is consulted before any rule — a tax-on-hold LC code
    short-circuits with ``kind='blocked'`` and the configured reason
    ends up in the audit log.  ``no_match`` is the explicit fallback;
    callers should treat it as an exception so finance gets a chance
    to add the missing rule rather than silently letting the asset
    flow through the default entity-resolver path.
    """

    rules: tuple[RoutingRule, ...]
    blocked_locations: dict[str, str] = field(default_factory=dict)

    def resolve(
        self,
        *,
        location: str | None,
        category_id: int | None,
        cost_centre: str | None = None,
    ) -> RoutingDecision:
        """Look up the routing decision for one asset."""
        loc_key = (location or "").strip().upper()
        if loc_key and loc_key in self.blocked_locations:
            return RoutingDecision(
                kind="blocked",
                reason=self.blocked_locations[loc_key],
            )
        for rule in self.rules:
            if rule.matches(
                location=location,
                category_id=category_id,
                cost_centre=cost_centre,
            ):
                log.info(
                    "Routing rule '%s' matched: location=%s category_id=%s "
                    "cost_centre=%s -> %s",
                    rule.name,
                    location,
                    category_id,
                    cost_centre,
                    rule.target_book,
                )
                return RoutingDecision(
                    kind="route_to",
                    target_book=rule.target_book,
                    rule_name=rule.name,
                )
        return RoutingDecision(
            kind="no_match",
            reason=(
                f"no routing rule matched: location={location!r} "
                f"category_id={category_id!r} cost_centre={cost_centre!r}"
            ),
        )

    @classmethod
    def from_config(cls, block: dict[str, Any]) -> "RoutingRulesResolver":
        """Build from the ``routing_rules`` config block.

        Expected shape::

            {
              "blocked_locations": { "LC000099": "KL — tax pending" },
              "rules": [
                { "name": "AMS_CSNL_office",
                  "location_in": ["LC000030"],
                  "category_in": [82001, 82002],
                  "target_book": "CSNL CORP BOOK" },
                ...
              ]
            }
        """
        rules_raw = block.get("rules") or []
        blocked_raw = block.get("blocked_locations") or {}
        rules = tuple(RoutingRule.from_dict(r) for r in rules_raw)
        blocked = {str(k).strip().upper(): str(v) for k, v in blocked_raw.items()}
        return cls(rules=rules, blocked_locations=blocked)
