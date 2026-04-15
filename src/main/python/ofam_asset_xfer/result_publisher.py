"""Abstract result publisher protocol.

Decouples the sync engine from any specific output destination (GCS, BigQuery,
local disk, etc.).  The sync engine calls ``publish()`` with structured results;
the concrete implementation decides where they go.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class ResultPublisher(Protocol):
    """Publishes transfer results to an external sink."""

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Persist *summary* and per-asset *results* to the configured sink."""
        ...
