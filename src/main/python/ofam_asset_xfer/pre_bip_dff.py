"""Pre-BIP descriptive-flexfield update stage for OFAM.

This module adds an optional *pre-stage* before the existing BIP-driven
transfer scan:

    upstream CMDB delta -> update Fusion asset descriptive details -> run BIP

The current transfer engine starts at BI Publisher. That means any transfer-
driving DFF values must already be visible in Fusion by the time the report
runs. This module lets callers push those values first through the supported
Fixed Assets ERP Integrations handle
``processTransaction-updateAssetDescriptiveDetails``.

Because tenants expose custom DFF segments differently, the actual Fusion
parameter names are fully config-driven. Example mapping::

    {
      "transfer_to_entity": "P_ATTRIBUTE15",
      "transfer_date": "P_ATTRIBUTE_DATE1",
      "transfer_to_location": "P_ATTRIBUTE16",
      "book_type_code": "P_BOOK_TYPE_CODE"
    }

The caller supplies *logical* field names in each request; this module maps
those onto the configured Fusion parameter names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .exceptions import ConfigError, FusionApiError, ValidationError
from .fusion_ops import get_asset_information
from .oracle_client import OracleErpIntegrationsClient


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DffUpdateRequest:
    """One requested pre-BIP DFF update for a fixed asset."""

    request_id: str
    asset_number: str
    book_type_code: str
    fields: Dict[str, Any]
    asset_id: Optional[str] = None

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DffUpdateRequest":
        """Parse a dictionary into a DffUpdateRequest instance."""
        if not isinstance(data, dict):
            raise ConfigError("Each pre_bip_dff_updates.requests item must be an object.")

        request_id = str(data.get("request_id") or "").strip()
        asset_number = str(data.get("asset_number") or "").strip()
        book_type_code = str(data.get("book_type_code") or "").strip()
        asset_id = str(data.get("asset_id") or "").strip() or None

        fields_raw = data.get("fields")
        if fields_raw is None:
            reserved = {"request_id", "asset_id", "asset_number", "book_type_code"}
            fields_raw = {k: v for k, v in data.items() if k not in reserved}

        if not request_id:
            raise ConfigError("pre_bip_dff update request is missing request_id.")
        if not asset_number:
            raise ConfigError(
                f"pre_bip_dff update request {request_id!r} is missing asset_number."
            )
        if not book_type_code:
            raise ConfigError(
                f"pre_bip_dff update request {request_id!r} is missing book_type_code."
            )
        if not isinstance(fields_raw, dict):
            raise ConfigError(
                f"pre_bip_dff update request {request_id!r} fields must be an object."
            )

        return DffUpdateRequest(
            request_id=request_id,
            asset_id=asset_id,
            asset_number=asset_number,
            book_type_code=book_type_code,
            fields=dict(fields_raw),
        )


@dataclass(frozen=True)
class DffUpdateResult:
    """Outcome of one pre-BIP DFF update attempt."""

    request_id: str
    asset_number: str
    book_type_code: str
    status: str  # UPDATED, DRY_RUN, FAILED
    asset_id: Optional[str] = None
    error: Optional[str] = None
    fusion_response: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class PreBipDffConfig:
    """Configuration for the pre-BIP DFF update stage."""

    enabled: bool = False
    operation_handle: str = "updateAssetDescriptiveDetails"
    field_parameter_map: Dict[str, str] = field(default_factory=dict)
    static_params: Dict[str, Any] = field(default_factory=dict)
    field_defaults: Dict[str, Any] = field(default_factory=dict)
    request_id_param: str = "P_TRX_ATTRIBUTE1"
    trace_param: str = "P_TRX_ATTRIBUTE2"
    trace_value: str = "OFAM_PRE_BIP_DFF"
    lookup_asset_id_when_missing: bool = True
    continue_on_error: bool = True
    bip_visibility_delay_seconds: float = 0.0

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "PreBipDffConfig":
        """Parse a dictionary into a PreBipDffConfig instance."""
        if not isinstance(data, dict):
            raise ConfigError("pre_bip_dff_updates must be an object.")

        field_parameter_map = data.get("field_parameter_map") or {}
        if not isinstance(field_parameter_map, dict):
            raise ConfigError("pre_bip_dff_updates.field_parameter_map must be an object.")
        for logical_name, fusion_param in field_parameter_map.items():
            if not str(logical_name).strip():
                raise ConfigError("pre_bip_dff_updates.field_parameter_map has a blank key.")
            param = str(fusion_param).strip()
            if not param:
                raise ConfigError(
                    "pre_bip_dff_updates.field_parameter_map contains a blank Fusion parameter name."
                )
            if param.upper() != param:
                raise ConfigError(
                    f"Fusion parameter names must be uppercase: {param!r}."
                )

        static_params = data.get("static_params") or {}
        if not isinstance(static_params, dict):
            raise ConfigError("pre_bip_dff_updates.static_params must be an object.")
        for name in static_params.keys():
            key = str(name).strip()
            if not key:
                raise ConfigError("pre_bip_dff_updates.static_params contains a blank key.")
            if key.upper() != key:
                raise ConfigError(
                    f"Static Fusion parameter names must be uppercase: {key!r}."
                )

        field_defaults = data.get("field_defaults") or {}
        if not isinstance(field_defaults, dict):
            raise ConfigError("pre_bip_dff_updates.field_defaults must be an object.")

        return PreBipDffConfig(
            enabled=bool(data.get("enabled", False)),
            operation_handle=str(
                data.get("operation_handle") or "updateAssetDescriptiveDetails"
            ).strip(),
            field_parameter_map={str(k): str(v).strip() for k, v in field_parameter_map.items()},
            field_defaults={str(k): v for k, v in field_defaults.items()},
            static_params=dict(static_params),
            request_id_param=str(data.get("request_id_param") or "P_TRX_ATTRIBUTE1").strip(),
            trace_param=str(data.get("trace_param") or "P_TRX_ATTRIBUTE2").strip(),
            trace_value=str(data.get("trace_value") or "OFAM_PRE_BIP_DFF").strip(),
            lookup_asset_id_when_missing=bool(data.get("lookup_asset_id_when_missing", True)),
            continue_on_error=bool(data.get("continue_on_error", True)),
            bip_visibility_delay_seconds=float(data.get("bip_visibility_delay_seconds", 0.0)),
        )


def load_pre_bip_dff_requests(config_block: Dict[str, Any]) -> List[DffUpdateRequest]:
    """Parse ``pre_bip_dff_updates.requests`` into dataclasses."""
    raw = config_block.get("requests") or []
    if not isinstance(raw, list):
        raise ConfigError("pre_bip_dff_updates.requests must be a list.")
    return [DffUpdateRequest.from_dict(item) for item in raw]


class PreBipDffUpdater:
    """Apply descriptive-detail updates before the BIP report runs."""

    def __init__(
        self,
        fusion_client: OracleErpIntegrationsClient,
        cfg: PreBipDffConfig,
    ):
        self._client = fusion_client
        self._cfg = cfg

    def apply_updates(
        self,
        requests: List[DffUpdateRequest],
        *,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """Apply a batch of pre-BIP DFF updates.

        Returns a summary dictionary suitable for JSON serialization.
        """
        counts = {
            "total": len(requests),
            "updated": 0,
            "failed": 0,
            "dry_run": 0,
        }
        results: List[Dict[str, Any]] = []

        for req in requests:
            try:
                result = self.apply_update(req, dry_run=dry_run)
            except Exception as exc:
                if not self._cfg.continue_on_error:
                    raise
                result = DffUpdateResult(
                    request_id=req.request_id,
                    asset_number=req.asset_number,
                    book_type_code=req.book_type_code,
                    asset_id=req.asset_id,
                    status="FAILED",
                    error=str(exc),
                )

            results.append(
                {
                    "request_id": result.request_id,
                    "asset_id": result.asset_id,
                    "asset_number": result.asset_number,
                    "book_type_code": result.book_type_code,
                    "status": result.status,
                    "error": result.error,
                    "fusion_response": result.fusion_response,
                }
            )

            key = result.status.lower()
            if key in counts:
                counts[key] += 1

        return {
            "enabled": self._cfg.enabled,
            "operation_handle": self._cfg.operation_handle,
            "counts": counts,
            "results": results,
        }

    def apply_update(
        self,
        request: DffUpdateRequest,
        *,
        dry_run: bool = True,
    ) -> DffUpdateResult:
        """Apply one pre-BIP DFF update."""
        asset_id = request.asset_id or self._resolve_asset_id(request)
        params = self._build_params(request, asset_id)

        if dry_run:
            return DffUpdateResult(
                request_id=request.request_id,
                asset_id=asset_id,
                asset_number=request.asset_number,
                book_type_code=request.book_type_code,
                status="DRY_RUN",
                fusion_response={"planned_params": params},
            )

        raw, pl = self._client.process_transaction(self._cfg.operation_handle, params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()
        status = "UPDATED" if status_code == "S" else "FAILED"
        error = None if status == "UPDATED" else f"Fusion X_RETURN_STATUS={status_code}"

        return DffUpdateResult(
            request_id=request.request_id,
            asset_id=asset_id,
            asset_number=request.asset_number,
            book_type_code=request.book_type_code,
            status=status,
            error=error,
            fusion_response=pl or raw,
        )

    def _resolve_asset_id(self, request: DffUpdateRequest) -> str:
        if not self._cfg.lookup_asset_id_when_missing:
            raise ValidationError(
                f"Asset ID is required for pre-BIP DFF update request {request.request_id!r}."
            )

        try:
            _raw, _pl, state = get_asset_information(
                self._client,
                request.book_type_code,
                request.asset_number,
            )
        except FusionApiError as exc:
            raise FusionApiError(
                f"Failed to resolve asset_id for request {request.request_id!r} "
                f"({request.book_type_code}/{request.asset_number}): {exc}"
            ) from exc

        return state.asset_id

    def _build_params(self, request: DffUpdateRequest, asset_id: str) -> Dict[str, Any]:
        params: Dict[str, Any] = dict(self._cfg.static_params)
        params["P_ASSET_ID"] = asset_id
        if self._cfg.request_id_param:
            params[self._cfg.request_id_param] = request.request_id
        if self._cfg.trace_param:
            params[self._cfg.trace_param] = self._cfg.trace_value

        available_values: Dict[str, Any] = {
            "request_id": request.request_id,
            "asset_id": asset_id,
            "asset_number": request.asset_number,
            "book_type_code": request.book_type_code,
        }
        # Apply field_defaults first, then overlay with request fields
        # so explicit values always win over defaults.
        available_values.update(self._cfg.field_defaults)
        available_values.update(request.fields)

        mapped_count = 0
        for logical_name, fusion_param in self._cfg.field_parameter_map.items():
            if logical_name not in available_values:
                continue
            value = available_values[logical_name]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            params[fusion_param] = value
            mapped_count += 1

        if mapped_count == 0:
            raise ValidationError(
                f"No mapped pre-BIP DFF fields were present for request {request.request_id!r}. "
                f"Configured logical fields: {sorted(self._cfg.field_parameter_map.keys())}"
            )

        return params
