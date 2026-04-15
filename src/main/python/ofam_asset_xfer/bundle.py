# =============================================================================
# MERGED FILE — 3 original modules joined as-is with section markers.
#
#   Section 1 → proxy_config.py
#   Section 2 → bip_client.py
#   Section 3 → oracle_client.py
#
# To find section boundaries:
#   grep -n "^# >>> FILE\|^# <<< END" bundle.py
# =============================================================================

# >>> FILE: proxy_config.py >>>
"""Proxy configuration for Oracle URL access.

Reads INETPROXY_USER, INETPROXY_PASSWD, HTTP_APP_PROXY, and HTTPS_APP_PROXY
from environment variables and builds authenticated proxy URLs for use with
the ``requests`` library.

When the environment variables are not set, returns an empty dict (no proxy).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional
from urllib.parse import quote_plus

log = logging.getLogger(__name__)


def get_proxy_config(require: bool = False) -> Dict[str, str]:
    """Build a ``requests``-compatible proxies dict from environment variables.

    Expected env vars:
        INETPROXY_USER     – proxy username
        INETPROXY_PASSWD   – proxy password
        HTTP_APP_PROXY     – HTTP proxy URL  (e.g. http://proxy.example.com:80)
        HTTPS_APP_PROXY    – HTTPS proxy URL (e.g. http://proxy.example.com:443)

    Args:
        require: If True, raise ValueError when proxy env vars are missing
                 instead of silently returning an empty dict.  Use this in
                 pod/batch environments where direct routes don't exist.

    Returns:
        Dict suitable for ``requests.Session.proxies`` or ``requests.get(proxies=...)``.
        Empty dict when proxy env vars are absent and require=False.
    """
    http_proxy_url = os.environ.get("HTTP_APP_PROXY")
    https_proxy_url = os.environ.get("HTTPS_APP_PROXY")

    if not http_proxy_url and not https_proxy_url:
        if require:
            raise ValueError(
                "Proxy is required but HTTP_APP_PROXY / HTTPS_APP_PROXY env vars "
                "are not set. Set them or pass require=False for local dev."
            )
        log.debug("No proxy env vars found (HTTP_APP_PROXY / HTTPS_APP_PROXY); skipping proxy.")
        return {}

    user = os.environ.get("INETPROXY_USER", "")
    passwd = os.environ.get("INETPROXY_PASSWD", "")

    if not user or not passwd:
        if require:
            raise ValueError(
                "Proxy env vars set but INETPROXY_USER / INETPROXY_PASSWD are "
                "missing. Check the K8s secret (Holocron Vault)."
            )
        log.warning("Proxy URLs set but INETPROXY_USER/PASSWD missing — proxy will be unauthenticated.")

    proxies: Dict[str, str] = {}

    if http_proxy_url:
        proxies["http"] = _build_proxy_url(user, passwd, http_proxy_url)

    if https_proxy_url:
        proxies["https"] = _build_proxy_url(user, passwd, https_proxy_url)

    log.info(
        "Proxy configured for protocols: %s (user=%s)",
        list(proxies.keys()),
        user or "<none>",
    )
    return proxies


def _build_proxy_url(user: str, passwd: str, raw_url: str) -> str:
    """Insert ``user:password@`` into *raw_url* when credentials are provided.

    Example:
        _build_proxy_url("alice", "p@ss", "http://proxy.corp:8080")
        → "http://alice:p%40ss@proxy.corp:8080"
    """
    if not user:
        return raw_url

    # Strip the scheme so we can re-prefix with credentials.
    host_part = raw_url.replace("http://", "").replace("https://", "")
    return f"http://{quote_plus(user)}:{quote_plus(passwd)}@{host_part}"
# <<< END: proxy_config.py <<<

# >>> FILE: bip_client.py >>>
"""BI Publisher SOAP client for running reports.

Calls the ExternalReportWSSService SOAP 1.2 endpoint to run a BI Publisher
report and returns the decoded XML output as a list of row dicts.

Authentication uses Bearer token (same as Fusion REST).

Usage:
    cfg = BIPConfig(base_url="https://fa-host.oraclecloud.com",
                    bearer_token="...", report_path="/Custom/.../Report.xdo")
    client = BIPClient(cfg)
    rows = client.run_report(params={"P_BOOK_TYPE_CODE": "US CORP BOOK"})
    # rows = [{"ASSET_NUMBER": "142847", "BOOK_TYPE_CODE": "US CORP BOOK", ...}, ...]
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests  # type: ignore[import-untyped]

from .exceptions import FusionApiError
from .proxy_config import get_proxy_config


log = logging.getLogger(__name__)

# SOAP 1.2 / BIP namespaces
_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
_PUB_NS = "http://xmlns.oracle.com/oxp/service/PublicReportService"


@dataclass(frozen=True)
class BIPConfig:
    """Configuration for BI Publisher SOAP client."""

    base_url: str  # e.g. https://fa-host.oraclecloud.com
    bearer_token: str  # same Fusion JWT / Bearer token
    report_path: str  # e.g. /Custom/Integrations/Outbound/FA/AssetTransferDFF.xdo
    verify_ssl: bool = True
    timeout_seconds: int = 120
    require_proxy: bool = False

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BIPConfig":
        """Build a BIPConfig from a raw config dictionary."""
        base_url = str(d.get("base_url", "")).rstrip("/")
        report_path = str(d.get("report_path", "")).strip()

        bearer_token = d.get("bearer_token")
        bearer_token_env = d.get("bearer_token_env")
        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        if not base_url:
            raise ValueError("bip.base_url is required")
        if not bearer_token:
            raise ValueError(
                "bip.bearer_token is required (bearer_token or bearer_token_env)"
            )
        if not report_path:
            raise ValueError("bip.report_path is required")

        return BIPConfig(
            base_url=base_url,
            bearer_token=str(bearer_token),
            report_path=report_path,
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 120)),
            require_proxy=bool(d.get("require_proxy", False)),
        )


class BIPClient:
    """Client for Oracle BI Publisher SOAP reporting service."""

    def __init__(self, cfg: BIPConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.trust_env = not cfg.require_proxy
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/soap+xml; charset=utf-8",
            }
        )
        self._session.proxies.update(get_proxy_config(require=cfg.require_proxy))

    def _endpoint(self) -> str:
        return f"{self.cfg.base_url}/xmlpserver/services/ExternalReportWSSService"

    def run_report(
        self,
        params: Optional[Dict[str, str]] = None,
        report_path: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Run a BIP report and return parsed rows as list of dicts.

        Args:
            params: Optional report parameters as name -> value.
            report_path: Override the default report path from config.

        Returns:
            List of dicts, one per G_1 row, keyed by XML element names.
        """
        path = report_path or self.cfg.report_path
        report_bytes = self._call_run_report(path, params)
        return self._parse_data_ds(report_bytes)

    def _call_run_report(
        self,
        report_path: str,
        params: Optional[Dict[str, str]],
    ) -> bytes:
        """Build SOAP 1.2 envelope, POST to BIP, return decoded reportBytes."""
        # Build parameter XML fragment
        param_xml = ""
        if params:
            items = []
            for name, value in params.items():
                items.append(
                    "<pub:item>"
                    f"<pub:name>{_xml_escape(name)}</pub:name>"
                    f"<pub:values><pub:item>{_xml_escape(value)}</pub:item></pub:values>"
                    "</pub:item>"
                )
            param_xml = (
                "<pub:parameterNameValues>"
                + "".join(items)
                + "</pub:parameterNameValues>"
            )

        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{_SOAP_NS}" xmlns:pub="{_PUB_NS}">'
            "<soap:Header/>"
            "<soap:Body>"
            "<pub:runReport>"
            "<pub:reportRequest>"
            f"{param_xml}"
            f"<pub:reportAbsolutePath>{_xml_escape(report_path)}</pub:reportAbsolutePath>"
            "<pub:sizeOfDataChunkDownload>-1</pub:sizeOfDataChunkDownload>"
            "<pub:reportOutputPath/>"
            "</pub:reportRequest>"
            "</pub:runReport>"
            "</soap:Body>"
            "</soap:Envelope>"
        )

        url = self._endpoint()
        log.debug("BIP SOAP POST %s (report=%s)", url, report_path)

        resp = self._session.post(
            url,
            data=envelope.encode("utf-8"),
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )

        if resp.status_code >= 400:
            raise FusionApiError(f"BIP SOAP HTTP {resp.status_code}: {resp.text[:500]}")

        return self._extract_report_bytes(resp.content)

    @staticmethod
    def _extract_report_bytes(soap_response: bytes) -> bytes:
        """Extract and base64-decode reportBytes from SOAP response XML."""
        root = ET.fromstring(soap_response)

        # Search for reportBytes element across any namespace
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "reportBytes" and elem.text:
                return base64.b64decode(elem.text)

        raise FusionApiError("No reportBytes found in BIP SOAP response")

    @staticmethod
    def _parse_data_ds(data: bytes) -> List[Dict[str, str]]:
        """Parse BIP XML report output structured as DATA_DS/G_1 rows.

        Expected structure:
            <DATA_DS>
              <P_BOOK_TYPE_CODE>UK CORP BOOK</P_BOOK_TYPE_CODE>
              <G_1>
                <ASSET_NUMBER>142847</ASSET_NUMBER>
                <TRANSFER_TO_ENTITY>US Entity</TRANSFER_TO_ENTITY>
                ...
              </G_1>
              ...
            </DATA_DS>

        Root-level scalar elements (like echoed report parameters) are
        injected into every row dict so downstream code can reference them
        without special handling.

        Returns:
            List of dicts, one per G_1 element.
        """
        text = data.decode("utf-8-sig")
        root = ET.fromstring(text)

        # Collect root-level scalar elements (non-G_1 children of DATA_DS).
        # These are typically echoed report parameters like P_BOOK_TYPE_CODE.
        root_fields: Dict[str, str] = {}
        for child in root:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local != "G_1" and child.text is not None:
                root_fields[local] = child.text.strip()

        rows: List[Dict[str, str]] = []
        # Find all G_1 elements (could be at root level or under DATA_DS)
        g1_elements = root.findall(".//G_1")
        for g1 in g1_elements:
            row: Dict[str, str] = dict(root_fields)  # seed with root-level fields
            for child in g1:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                row[local] = (child.text or "").strip()
            rows.append(row)

        return rows


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
# <<< END: bip_client.py <<<

# >>> FILE: oracle_client.py >>>
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import requests  # type: ignore[import-untyped]

from .exceptions import FusionApiError
from .paramlist import build_parameter_list
from .proxy_config import get_proxy_config


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OracleConfig:
    base_url: str
    api_version: str
    bearer_token: str
    verify_ssl: bool = True
    timeout_seconds: int = 60
    require_proxy: bool = False

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OracleConfig":
        """Build an OracleConfig from a raw config dictionary."""
        base_url = str(d.get("base_url", "")).rstrip("/")
        api_version = str(d.get("api_version", "")).strip()
        if not base_url or not api_version:
            raise ValueError("oracle.base_url and oracle.api_version are required")

        # Bearer token: sourced from config or, preferably, from an env var
        # injected by CDX secrets at pod startup (bearer_token_env points to
        # the env var name, e.g. "FUSION_JWT").  In production the token is
        # rotated by the CDX secrets sidecar; for local dev it can be set
        # directly in the config as "bearer_token".
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
            require_proxy=bool(d.get("require_proxy", False)),
        )


class OracleErpIntegrationsClient:
    """Client for Oracle Fusion ERP Integration REST Service for Assets transactions."""

    def __init__(self, cfg: OracleConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.trust_env = not cfg.require_proxy
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
                "REST-header-version": "4",
                "ACCEPT": "application/json",
            }
        )
        self._session.proxies.update(get_proxy_config(require=cfg.require_proxy))

    def _endpoint(self, handle: str) -> str:
        # Example from doc: /fscmRestApi/resources/11.13.18.05/erpintegrations/processTransaction-transferAsset
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/erpintegrations"
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

        return dict(raw)
# <<< END: oracle_client.py <<<
