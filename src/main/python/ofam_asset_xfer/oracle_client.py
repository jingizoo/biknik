from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import requests

from .exceptions import FusionApiError
from .paramlist import build_parameter_list


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OracleConfig:
    base_url: str
    api_version: str
    bearer_token: str
    verify_ssl: bool = True
    timeout_seconds: int = 60

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OracleConfig":
        base_url = str(d.get("base_url", "")).rstrip("/")
        api_version = str(d.get("api_version", "")).strip()
        if not base_url or not api_version:
            raise ValueError("oracle.base_url and oracle.api_version are required")

        # Prefer env indirection for token (CDX secrets pattern)
        bearer_token = d.get("bearer_token")
        bearer_token_env = d.get("bearer_token_env")

        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        if not bearer_token:
            raise ValueError(
                "Oracle bearer_token is required (bearer_token or bearer_token_env)."
            )

        return OracleConfig(
            base_url=base_url,
            api_version=api_version,
            bearer_token=str(bearer_token),
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
        )


class OracleErpIntegrationsClient:
    """Client for Oracle Fusion ERP Integration REST Service for Assets transactions."""

    def __init__(self, cfg: OracleConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
                "REST-header-version": "4",
                "ACCEPT": "application/json",
            }
        )

    def _endpoint(self, handle: str) -> str:
        # Example from doc: /fscmRestApi/resources/11.13.18.05/erpintegrations/processTransaction-transferAsset
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/erpintegrations/processTransaction-{handle}"
        return self.cfg.base_url + rel

    def _resource_url(self, resource_path: str) -> str:
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/{resource_path}"
        return self.cfg.base_url + rel

    def process_transaction(
        self, handle: str, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """POST processTransaction-<handle>.

        Returns:
          (raw_response_json, parsed_parameter_list_dict)
        """
        op_name = f"processTransaction-{handle}"
        payload = {
            "OperationName": op_name,
            "ParameterList": build_parameter_list(params),
        }

        url = self._endpoint(handle)
        log.debug("POST %s payload=%s", url, payload)

        r = self._session.post(
            url,
            json=payload,
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )
        try:
            raw = r.json()
        except Exception as e:
            raise FusionApiError(
                f"Non-JSON response from Fusion (status={r.status_code}): {r.text[:500]}"
            ) from e

        if r.status_code >= 400:
            raise FusionApiError(f"Fusion HTTP {r.status_code}: {raw}")

        pl_raw = raw.get("ParameterList")
        pl: Dict[str, Any] = {}
        if isinstance(pl_raw, str) and pl_raw.strip():
            try:
                pl = json.loads(pl_raw)
            except Exception:
                # Some handles may return a non-JSON string ParameterList. Preserve it.
                pl = {"_raw": pl_raw}
        elif isinstance(pl_raw, dict):
            pl = pl_raw

        return raw, pl

    def get_resource(
        self, resource_path: str, query_params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """GET a Fusion REST resource (e.g. accountCombinationsLOV).

        Returns the parsed JSON response body.
        """
        url = self._resource_url(resource_path)
        log.debug("GET %s params=%s", url, query_params)

        r = self._session.get(
            url,
            params=query_params or {},
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )
        try:
            raw = r.json()
        except Exception as e:
            raise FusionApiError(
                f"Non-JSON response from Fusion GET (status={r.status_code}): {r.text[:500]}"
            ) from e

        if r.status_code >= 400:
            raise FusionApiError(f"Fusion GET HTTP {r.status_code}: {raw}")

        return raw
