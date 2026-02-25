from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
from requests.auth import HTTPBasicAuth

from .exceptions import FusionApiError
from .paramlist import build_parameter_list


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OracleConfig:
    base_url: str
    api_version: str
    username: str
    password: str
    verify_ssl: bool = True
    timeout_seconds: int = 60

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OracleConfig":
        base_url = str(d.get("base_url", "")).rstrip("/")
        api_version = str(d.get("api_version", "")).strip()
        if not base_url or not api_version:
            raise ValueError("oracle.base_url and oracle.api_version are required")

        # Prefer env indirection for credentials (CDX secrets pattern)
        username = d.get("username")
        password = d.get("password")
        username_env = d.get("username_env")
        password_env = d.get("password_env")

        if username_env:
            username = os.getenv(str(username_env), username)
        if password_env:
            password = os.getenv(str(password_env), password)

        if not username or not password:
            raise ValueError("Oracle credentials are required (username/password or username_env/password_env).")

        return OracleConfig(
            base_url=base_url,
            api_version=api_version,
            username=str(username),
            password=str(password),
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
        )


class OracleErpIntegrationsClient:
    """Client for Oracle Fusion ERP Integration REST Service for Assets transactions."""

    def __init__(self, cfg: OracleConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(cfg.username, cfg.password)
        self._session.headers.update({
            "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
            "REST-header-version": "4",
            "ACCEPT": "application/json",
        })

    def _endpoint(self, handle: str) -> str:
        # Example from doc: /fscmRestApi/resources/11.13.18.05/erpintegrations/processTransaction-transferAsset
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/erpintegrations/processTransaction-{handle}"
        return self.cfg.base_url + rel

    def process_transaction(self, handle: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
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
            raise FusionApiError(f"Non-JSON response from Fusion (status={r.status_code}): {r.text[:500]}") from e

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
