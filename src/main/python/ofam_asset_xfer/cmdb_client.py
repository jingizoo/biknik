"""CMDB (ServiceNow) REST API client for asset data retrieval.

Connects to the ServiceNow CMDB table API to fetch hardware asset records.
The primary use case is pulling asset location data (u_cit_location_code)
for comparison against Oracle Fusion FA locations.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

from .exceptions import CMDBApiError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CMDBConfig:
    """Connection configuration for ServiceNow CMDB API."""

    base_url: str                       # e.g. https://instance.service-now.com
    username: str
    password: str
    table: str = "alm_hardware"         # ServiceNow table name
    verify_ssl: bool = True
    timeout_seconds: int = 60

    # Fields to retrieve from CMDB (reduces payload size)
    fields: tuple = (
        "sys_id",
        "asset_tag",
        "display_name",
        "serial_number",
        "u_cit_location_code",
        "install_location",
        "location",
        "company",
        "assigned_to",
        "model_category",
        "substatus",
        "install_status",
    )

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CMDBConfig":
        base_url = str(d.get("base_url", "")).rstrip("/")
        if not base_url:
            raise ValueError("cmdb.base_url is required")

        username = d.get("username")
        password = d.get("password")
        username_env = d.get("username_env")
        password_env = d.get("password_env")

        if username_env:
            username = os.getenv(str(username_env), username)
        if password_env:
            password = os.getenv(str(password_env), password)

        if not username or not password:
            raise ValueError("CMDB credentials are required (username/password or username_env/password_env).")

        table = str(d.get("table", "alm_hardware")).strip()
        fields_raw = d.get("fields")
        fields = tuple(fields_raw) if isinstance(fields_raw, (list, tuple)) else CMDBConfig.fields

        return CMDBConfig(
            base_url=base_url,
            username=str(username),
            password=str(password),
            table=table,
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            fields=fields,
        )


class CMDBClient:
    """Client for ServiceNow CMDB Table API."""

    def __init__(self, cfg: CMDBConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(cfg.username, cfg.password)
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _table_url(self) -> str:
        return f"{self.cfg.base_url}/api/now/table/{self.cfg.table}"

    def get_assets(
        self,
        query: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Fetch a page of hardware assets from CMDB.

        Args:
            query: ServiceNow encoded query string (e.g. "install_status=1^substatus=In use")
            limit: Max records per page
            offset: Record offset for pagination

        Returns:
            List of asset dicts with requested fields.
        """
        params: Dict[str, Any] = {
            "sysparm_limit": limit,
            "sysparm_offset": offset,
            "sysparm_fields": ",".join(self.cfg.fields),
        }
        if query:
            params["sysparm_query"] = query

        url = self._table_url()
        log.debug("GET %s params=%s", url, params)

        r = self._session.get(
            url,
            params=params,
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )

        try:
            raw = r.json()
        except Exception as e:
            raise CMDBApiError(
                f"Non-JSON response from CMDB (status={r.status_code}): {r.text[:500]}"
            ) from e

        if r.status_code >= 400:
            raise CMDBApiError(f"CMDB HTTP {r.status_code}: {raw}")

        result = raw.get("result", [])
        log.info("CMDB returned %d assets (offset=%d, limit=%d)", len(result), offset, limit)
        return result

    def get_asset_by_tag(self, asset_tag: str) -> Optional[Dict[str, Any]]:
        """Look up a single asset by its asset_tag field."""
        assets = self.get_assets(
            query=f"asset_tag={asset_tag}",
            limit=1,
        )
        return assets[0] if assets else None

    def get_all_assets(
        self,
        query: Optional[str] = None,
        batch_size: int = 100,
        max_records: int = 10000,
    ) -> List[Dict[str, Any]]:
        """Paginate through all matching assets up to max_records.

        Args:
            query: ServiceNow encoded query filter
            batch_size: Records per API call
            max_records: Safety limit to prevent runaway queries

        Returns:
            Aggregated list of asset dicts.
        """
        all_assets: List[Dict[str, Any]] = []
        offset = 0

        while offset < max_records:
            page = self.get_assets(query=query, limit=batch_size, offset=offset)
            if not page:
                break
            all_assets.extend(page)
            if len(page) < batch_size:
                break
            offset += batch_size

        log.info("CMDB total assets fetched: %d", len(all_assets))
        return all_assets

    def get_assets_with_location(
        self,
        additional_query: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Fetch active assets that have a u_cit_location_code set.

        This is the primary query for CMDB→FA location sync: we only care
        about assets that have an assignable location code.
        """
        base_query = "u_cit_location_codeISNOTEMPTY^install_status=1"
        if additional_query:
            base_query = f"{base_query}^{additional_query}"

        return self.get_all_assets(query=base_query, max_records=limit)
