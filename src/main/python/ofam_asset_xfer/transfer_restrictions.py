"""Transfer restriction and approval framework.

Per the BRD:

EMEA:
  - Jurisdictions where tax has not confirmed asset ownership should be blocked
    (e.g., KL was on hold).
  - Location-based entity mapping nuances (Amsterdam: CSNL vs MRIS-NL vs CETS).

APAC:
  - Transfers to/from the India Book require additional approval from IN controllers.
  - Transfers to/from the China Book require additional approval from CN controllers.

This module provides a configurable restriction engine that can block or flag
transfers requiring additional approval before execution.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from .exceptions import ValidationError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RestrictionResult:
    """Result of checking transfer restrictions."""

    allowed: bool
    requires_approval: bool = False
    approval_group: Optional[str] = None
    reason: Optional[str] = None
    restriction_type: Optional[str] = None  # "TAX_HOLD", "APPROVAL_REQUIRED", "BLOCKED"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
        }
        if self.approval_group:
            d["approval_group"] = self.approval_group
        if self.reason:
            d["reason"] = self.reason
        if self.restriction_type:
            d["restriction_type"] = self.restriction_type
        return d


@dataclass
class TransferRestrictionConfig:
    """Configuration for transfer restrictions.

    Attributes:
        blocked_books: Books where transfers are completely blocked
            (e.g., jurisdictions with unconfirmed tax ownership).
        approval_required_books: Books that require additional controller
            approval before transfer. Maps book code → approval group name.
        blocked_jurisdictions: Jurisdiction codes on tax hold.
        location_entity_rules: Location-based entity mapping overrides.
            Maps (location_pattern, cost_centre) → required entity.
    """

    blocked_books: Set[str] = field(default_factory=set)
    approval_required_books: Dict[str, str] = field(default_factory=dict)
    blocked_jurisdictions: Set[str] = field(default_factory=set)
    location_entity_rules: List[Dict[str, str]] = field(default_factory=list)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TransferRestrictionConfig":
        blocked_books = set()
        for b in d.get("blocked_books", []):
            blocked_books.add(str(b).upper().strip())

        approval_required = {}
        for book, group in d.get("approval_required_books", {}).items():
            approval_required[str(book).upper().strip()] = str(group)

        blocked_jurisdictions = set()
        for j in d.get("blocked_jurisdictions", []):
            blocked_jurisdictions.add(str(j).upper().strip())

        location_rules = d.get("location_entity_rules", [])

        return TransferRestrictionConfig(
            blocked_books=blocked_books,
            approval_required_books=approval_required,
            blocked_jurisdictions=blocked_jurisdictions,
            location_entity_rules=location_rules,
        )


class TransferRestrictionEngine:
    """Evaluates transfer restrictions before execution.

    Checks both source and target books against configured restrictions.
    """

    def __init__(self, config: TransferRestrictionConfig):
        self._config = config

    def check_transfer(
        self,
        source_book: str,
        target_book: str,
        source_jurisdiction: Optional[str] = None,
        target_jurisdiction: Optional[str] = None,
    ) -> RestrictionResult:
        """Check whether a transfer between two books is allowed.

        Returns a RestrictionResult indicating if the transfer is allowed,
        needs approval, or is blocked.
        """
        src = source_book.upper().strip()
        tgt = target_book.upper().strip()

        # Check blocked books
        if src in self._config.blocked_books:
            return RestrictionResult(
                allowed=False,
                restriction_type="BLOCKED",
                reason=f"Source book '{source_book}' is blocked for transfers.",
            )
        if tgt in self._config.blocked_books:
            return RestrictionResult(
                allowed=False,
                restriction_type="BLOCKED",
                reason=f"Target book '{target_book}' is blocked for transfers.",
            )

        # Check blocked jurisdictions (EMEA tax holds)
        if source_jurisdiction:
            sj = source_jurisdiction.upper().strip()
            if sj in self._config.blocked_jurisdictions:
                return RestrictionResult(
                    allowed=False,
                    restriction_type="TAX_HOLD",
                    reason=(
                        f"Source jurisdiction '{source_jurisdiction}' is on tax hold. "
                        f"Tax has not confirmed asset ownership."
                    ),
                )
        if target_jurisdiction:
            tj = target_jurisdiction.upper().strip()
            if tj in self._config.blocked_jurisdictions:
                return RestrictionResult(
                    allowed=False,
                    restriction_type="TAX_HOLD",
                    reason=(
                        f"Target jurisdiction '{target_jurisdiction}' is on tax hold. "
                        f"Tax has not confirmed asset ownership."
                    ),
                )

        # Check approval-required books (APAC: India, China)
        if src in self._config.approval_required_books:
            group = self._config.approval_required_books[src]
            log.info(
                "Transfer from '%s' requires approval from %s", source_book, group
            )
            return RestrictionResult(
                allowed=True,
                requires_approval=True,
                approval_group=group,
                restriction_type="APPROVAL_REQUIRED",
                reason=(
                    f"Transfers from '{source_book}' require additional "
                    f"approval from {group} controllers."
                ),
            )
        if tgt in self._config.approval_required_books:
            group = self._config.approval_required_books[tgt]
            log.info(
                "Transfer to '%s' requires approval from %s", target_book, group
            )
            return RestrictionResult(
                allowed=True,
                requires_approval=True,
                approval_group=group,
                restriction_type="APPROVAL_REQUIRED",
                reason=(
                    f"Transfers to '{target_book}' require additional "
                    f"approval from {group} controllers."
                ),
            )

        return RestrictionResult(allowed=True)

    def resolve_entity_by_location(
        self,
        location: str,
        cost_centre: Optional[str] = None,
        asset_type: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve the owning entity based on location and cost centre.

        Per BRD EMEA examples:
        - CSNL office tech: held by CSNL (location=selected, leasehold by cost centre)
        - MRIS-NL office tech: held by MRIS-NL (location=selected, leasehold by cost centre)
        - Servers/trading tech: held by CETS
        - Switzerland office: CSZH (location + department)
        - Switzerland servers: CETS (location)

        Returns the entity code if a rule matches, or None if no rule applies.
        """
        loc_upper = location.upper().strip() if location else ""

        for rule in self._config.location_entity_rules:
            rule_location = rule.get("location_pattern", "").upper().strip()
            rule_cost_centre = rule.get("cost_centre", "").upper().strip()
            rule_asset_type = rule.get("asset_type", "").upper().strip()
            entity = rule.get("entity", "").strip()

            if not entity:
                continue

            # Match location pattern
            if rule_location and rule_location not in loc_upper:
                continue

            # Match cost centre if specified
            if rule_cost_centre and cost_centre:
                if rule_cost_centre != cost_centre.upper().strip():
                    continue
            elif rule_cost_centre and not cost_centre:
                continue

            # Match asset type if specified
            if rule_asset_type and asset_type:
                if rule_asset_type != asset_type.upper().strip():
                    continue
            elif rule_asset_type and not asset_type:
                continue

            log.info(
                "Location entity rule matched: location='%s', cost_centre='%s' → entity='%s'",
                location,
                cost_centre,
                entity,
            )
            return entity

        return None
