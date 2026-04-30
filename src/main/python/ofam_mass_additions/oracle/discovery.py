"""Discovery: where do NEW mass-addition IDs come from?

Oracle Fusion does not expose a REST resource for listing mass additions.
``processTransaction-getMassAddition`` reads one row by ID, but there is
no equivalent ``listMassAdditions`` operation.  Tenants therefore have to
provide IDs via one of:

  * ``ExplicitIdsDiscovery`` — a hard-coded list in config (good for
    pilot runs, smoke tests, or when an upstream system already knows
    which IDs are pending).
  * ``BipReportDiscovery``   — call a BIP report that filters NEW rows
    by book.  Not yet implemented; the report path / output shape is
    tenant-specific (Citadel will need to publish one and supply the
    ``report_path``).

Plug in others by implementing the :class:`MassAdditionDiscovery`
protocol and registering them in :func:`build_discovery`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ofam_mass_additions.exceptions import ConfigError


@runtime_checkable
class MassAdditionDiscovery(Protocol):
    """Returns the IDs to feed to ``getMassAddition`` for a given book."""

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> list[str]:
        ...


@dataclass(frozen=True)
class ExplicitIdsDiscovery:
    """Hand-fed list of mass-addition IDs (per book or shared across all).

    Config shape::

        "discovery": {
          "mode": "explicit_ids",
          "ids": [300100180277508, 300100180277509]
        }

    OR per-book::

        "discovery": {
          "mode": "explicit_ids",
          "ids_by_book": {
            "US CORP BOOK": [300100180277508],
            "UK CORP BOOK": [300100180277510]
          }
        }
    """

    ids: tuple[str, ...] = ()
    ids_by_book: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExplicitIdsDiscovery":
        flat = tuple(str(x) for x in (d.get("ids") or []))
        per_book_raw = d.get("ids_by_book") or {}
        per_book = {
            str(book): tuple(str(x) for x in lst)
            for book, lst in per_book_raw.items()
        }
        if not flat and not per_book:
            raise ConfigError(
                "discovery.mode='explicit_ids' requires non-empty 'ids' or 'ids_by_book'."
            )
        return cls(ids=flat, ids_by_book=per_book or None)

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> list[str]:
        if self.ids_by_book is not None and book_type_code in self.ids_by_book:
            return list(self.ids_by_book[book_type_code])
        return list(self.ids)


@dataclass(frozen=True)
class BipReportDiscovery:
    """Discovery via a BI Publisher report (placeholder).

    Config shape (when implemented)::

        "discovery": {
          "mode": "bip_report",
          "report_path": "/Custom/.../New_Mass_Additions.xdo",
          "params": { "P_BOOK_TYPE_CODE": "US CORP BOOK" }
        }

    The shape mirrors the asset-xfer ``BIPClient`` (``ofam_asset_xfer.bip_client``).
    Until a real report exists in your Fusion tenant and we know the
    output format, calling this raises :class:`ConfigError` with a clear
    pointer.
    """

    report_path: str
    params: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BipReportDiscovery":
        report_path = str(d.get("report_path", "")).strip()
        if not report_path:
            raise ConfigError(
                "discovery.mode='bip_report' requires 'report_path'."
            )
        return cls(report_path=report_path, params=d.get("params") or {})

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> list[str]:
        raise ConfigError(
            "BIP-report-based discovery is not yet wired in ofam_mass_additions. "
            "Either (a) switch discovery.mode to 'explicit_ids' and supply "
            "the IDs you want to process, or (b) ask for BIP discovery to be "
            "implemented (would mirror ofam_asset_xfer.bip_client.BIPClient)."
        )


def build_discovery(d: dict[str, Any] | None) -> MassAdditionDiscovery:
    """Factory: build the right discovery from the config block.

    Defaults to ``explicit_ids`` so a missing ``mode`` still produces a
    helpful error if no IDs are supplied.
    """
    block = d or {}
    mode = str(block.get("mode", "explicit_ids")).strip().lower()
    if mode == "explicit_ids":
        return ExplicitIdsDiscovery.from_dict(block)
    if mode == "bip_report":
        return BipReportDiscovery.from_dict(block)
    raise ConfigError(
        f"Unknown discovery.mode={mode!r}. "
        "Supported: 'explicit_ids', 'bip_report' (placeholder)."
    )
