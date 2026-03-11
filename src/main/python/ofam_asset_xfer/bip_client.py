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
        )


class BIPClient:
    """Client for Oracle BI Publisher SOAP reporting service."""

    def __init__(self, cfg: BIPConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/soap+xml; charset=utf-8",
            }
        )

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
