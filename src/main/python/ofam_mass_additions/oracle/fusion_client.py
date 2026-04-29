"""Real Oracle Fusion FA mass-additions client.

Replaces ``MockOracleFaClient`` for production runs.  Two underlying
endpoints are wrapped:

1. **FA mass-additions REST resource** — used to *discover* and *read*
   rows in ``PostingStatus='NEW'`` for a given book.
2. **ERP Integrations processTransaction service** — used to *write*
   updates back via the documented FA operations:

       * ``processTransaction-updateMassAddition``  (FA_MASS_ADD_UPD_PUB)
       * ``processTransaction-getMassAddition``     (optional re-read)
       * ``processTransaction-changeMassAdditionsBook`` (future use)

Self-contained: no imports from ofam_asset_xfer.  Authentication is
bearer-token-from-env (the same ``FUSION_JWT`` env var the rest of the
estate already uses).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import requests

from ofam_mass_additions.exceptions import FusionApiError
from ofam_mass_additions.models import MassAddition
from ofam_mass_additions.oracle.paramlist import build_parameter_list
from ofam_mass_additions.proxy_config import get_proxy_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FusionFaConfig:
    """Configuration for :class:`FusionFaClient`.

    The ``*_attribute`` fields specify which DFF column on the
    FA mass-additions REST resource carries each business value (the
    runbook documents tag → ``Attribute1`` and serial → ``Attribute5``;
    other tenants may differ).

    The list/get path uses the REST resource at
    ``/fscmRestApi/resources/<api_version>/<mass_additions_resource>``;
    the update path uses ``/fscmRestApi/resources/<api_version>/erpintegrations``
    with an ``OperationName`` of ``processTransaction-<handle>``.
    """

    base_url: str
    api_version: str = "11.13.18.05"
    bearer_token_env: str = "FUSION_JWT"
    verify_ssl: bool = True
    require_proxy: bool = False
    timeout_seconds: int = 60

    mass_additions_resource: str = "FAmassAdditions"
    erp_integrations_resource: str = "erpintegrations"

    tag_number_attribute: str = "Attribute1"
    serial_number_attribute: str = "Attribute5"
    po_number_field: str = "PoNumber"
    invoice_number_field: str = "InvoiceNumber"
    cost_field: str = "Cost"
    quantity_field: str = "Quantity"
    region_field: str = "Country"

    page_size: int = 500

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FusionFaConfig":
        if not d.get("base_url"):
            raise ValueError("oracle.base_url is required for fusion mode")
        return cls(
            base_url=str(d["base_url"]).rstrip("/"),
            api_version=str(d.get("api_version", "11.13.18.05")),
            bearer_token_env=str(d.get("bearer_token_env", "FUSION_JWT")),
            verify_ssl=bool(d.get("verify_ssl", True)),
            require_proxy=bool(d.get("require_proxy", False)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            mass_additions_resource=str(d.get("mass_additions_resource", "FAmassAdditions")),
            erp_integrations_resource=str(d.get("erp_integrations_resource", "erpintegrations")),
            tag_number_attribute=str(d.get("tag_number_attribute", "Attribute1")),
            serial_number_attribute=str(d.get("serial_number_attribute", "Attribute5")),
            po_number_field=str(d.get("po_number_field", "PoNumber")),
            invoice_number_field=str(d.get("invoice_number_field", "InvoiceNumber")),
            cost_field=str(d.get("cost_field", "Cost")),
            quantity_field=str(d.get("quantity_field", "Quantity")),
            region_field=str(d.get("region_field", "Country")),
            page_size=int(d.get("page_size", 500)),
        )


class FusionFaClient:
    """Drop-in replacement for ``MockOracleFaClient`` against real Fusion."""

    def __init__(self, cfg: FusionFaConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.proxies = get_proxy_config(require=cfg.require_proxy)
        self._session.verify = cfg.verify_ssl
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
                "REST-header-version": "4",
            }
        )

    # ---- Discovery -------------------------------------------------------

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> List[str]:
        """Return MassAdditionId values for NEW rows in ``book_type_code``.

        Uses the REST resource with cursor-style pagination.
        """
        ids: List[str] = []
        offset = 0
        while True:
            body = self._get_resource(
                self._cfg.mass_additions_resource,
                params={
                    "q": (
                        f"PostingStatus='NEW' and BookTypeCode='{book_type_code}'"
                    ),
                    "fields": "MassAdditionId",
                    "limit": str(self._cfg.page_size),
                    "offset": str(offset),
                    "onlyData": "true",
                },
            )
            items = body.get("items") or []
            for item in items:
                ma_id = item.get("MassAdditionId")
                if ma_id is not None:
                    ids.append(str(ma_id))
            if not body.get("hasMore") or not items:
                break
            offset += len(items)
        return ids

    def get_mass_addition(self, mass_addition_id: str) -> MassAddition:
        """Fetch one mass-addition row from the REST resource and map to model."""
        body = self._get_resource(
            f"{self._cfg.mass_additions_resource}/{mass_addition_id}"
        )
        return self._row_to_mass_addition(body)

    # ---- Write -----------------------------------------------------------

    def update_mass_addition(self, payload: Dict[str, Any]) -> str:
        """Call ``processTransaction-updateMassAddition`` with ``payload``.

        Returns the raw ``ParameterList`` string from the response, matching
        the ``MockOracleFaClient`` contract used by ``parse_oracle_status``.
        """
        return self._process_transaction("updateMassAddition", payload)

    # ---- Internals -------------------------------------------------------

    def _get_resource(
        self, resource_path: str, params: Dict[str, str] | None = None
    ) -> Dict[str, Any]:
        url = (
            f"{self._cfg.base_url}/fscmRestApi/resources/"
            f"{self._cfg.api_version}/{resource_path}"
        )
        log.debug("GET %s params=%s", url, params)
        try:
            resp = self._session.get(
                url,
                params=params or {},
                headers=self._auth_headers(),
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FusionApiError(f"GET {url} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise FusionApiError(
                f"Fusion GET {resource_path} HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        try:
            return dict(resp.json())
        except Exception as exc:
            raise FusionApiError(
                f"Non-JSON response from Fusion GET {resource_path}: "
                f"{resp.text[:500]}"
            ) from exc

    def _process_transaction(self, handle: str, params: Dict[str, Any]) -> str:
        url = (
            f"{self._cfg.base_url}/fscmRestApi/resources/"
            f"{self._cfg.api_version}/{self._cfg.erp_integrations_resource}"
        )
        body = {
            "OperationName": f"processTransaction-{handle}",
            "ParameterList": build_parameter_list(params),
        }
        log.debug("POST %s payload=%s", url, body)
        try:
            resp = self._session.post(
                url,
                json=body,
                headers=self._auth_headers(),
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FusionApiError(f"POST {url} failed: {exc}") from exc

        if resp.status_code >= 400:
            raise FusionApiError(
                f"Fusion processTransaction-{handle} HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

        try:
            raw = resp.json()
        except Exception as exc:
            raise FusionApiError(
                f"Non-JSON response from processTransaction-{handle}: "
                f"{resp.text[:500]}"
            ) from exc

        pl_raw = raw.get("ParameterList")
        # The cycle runner expects the ParameterList string back; downstream
        # parse_oracle_status() handles JSON or `{X_RETURN_STATUS:'S', ...}`
        # styles equally.  We return whatever Fusion gave us, falling back
        # to the entire response body if the field was absent.
        if pl_raw is None:
            return json.dumps(raw)
        return pl_raw if isinstance(pl_raw, str) else json.dumps(pl_raw)

    def _auth_headers(self) -> Dict[str, str]:
        token = os.environ.get(self._cfg.bearer_token_env, "").strip()
        if not token:
            raise FusionApiError(
                f"Fusion bearer token env var '{self._cfg.bearer_token_env}' is empty."
            )
        return {"Authorization": f"Bearer {token}"}

    def _row_to_mass_addition(self, row: Dict[str, Any]) -> MassAddition:
        cfg = self._cfg
        return MassAddition(
            mass_addition_id=str(row.get("MassAdditionId", "")),
            book_type_code=str(row.get("BookTypeCode", "")),
            region=str(row.get(cfg.region_field, "") or ""),
            queue_name=str(row.get("QueueName", "NEW") or "NEW"),
            posting_status=str(row.get("PostingStatus", "NEW") or "NEW"),
            tag_number=_str_or_none(row.get(cfg.tag_number_attribute)),
            serial_number=_str_or_none(row.get(cfg.serial_number_attribute)),
            po_number=_str_or_none(row.get(cfg.po_number_field)),
            invoice_number=_str_or_none(row.get(cfg.invoice_number_field)),
            ship_to_location=_str_or_none(row.get("ShipToLocation")),
            delivery_location=_str_or_none(row.get("DeliveryLocation")),
            invoice_quantity=_int_or_none(row.get("InvoiceQuantity")),
            received_quantity=_int_or_none(row.get("ReceivedQuantity"))
            or _int_or_none(row.get(cfg.quantity_field)),
            cost=_float_or_none(row.get(cfg.cost_field)),
            source_system=str(row.get("SourceSystemCode", "OTBI") or "OTBI"),
        )


def _str_or_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _int_or_none(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
