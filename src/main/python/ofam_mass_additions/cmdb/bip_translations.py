"""BIP report loader for oracle_translations.

Replaces the static ``oracle_translations`` JSON block with live data
pulled from a BI Publisher report that joins FA_LOCATIONS and
GL_CODE_COMBINATIONS to ServiceNow CMDB codes.

Report contract (see sql/cmdb_oracle_translations.sql):
  Each G_1 row carries three columns:
    TRANSLATION_FIELD  -- 'location_id' | 'expense_ccid' | 'employee_id'
    CMDB_CODE          -- the string the ServiceNow client returns
    ORACLE_FA_ID       -- the Oracle FA numeric ID (as string in XML)

The loader builds and returns the same nested dict that the cycle runner
accepts for its ``oracle_translations`` parameter:
  { "location_id": { "LC000030": 300000004974106 },
    "expense_ccid": { "30755":   682610           },
    "employee_id":  {}                              }
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable
from xml.etree import ElementTree as ET

import requests

from ofam_mass_additions.exceptions import ConfigError
from ofam_mass_additions.proxy_config import get_proxy_config

log = logging.getLogger(__name__)

_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
_PUB_NS = "http://xmlns.oracle.com/oxp/service/PublicReportService"

_KNOWN_FIELDS = ("location_id", "expense_ccid", "employee_id")


@dataclass(frozen=True)
class BipTranslationsConfig:
    """Config for the BIP translations report client.

    Auth precedence: bearer_token_env → username + password_env.
    ``report_path`` must be the absolute Fusion BIP path, e.g.
    ``/Custom/Integrations/FA/CMDB_Oracle_Translations.xdo``.
    """

    base_url: str
    report_path: str
    book_type_code: str = "US CORP BOOK"
    bearer_token_env: str | None = None
    username: str | None = None
    password_env: str | None = None
    verify_ssl: bool = True
    require_proxy: bool = False
    timeout_seconds: int = 120

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BipTranslationsConfig":
        base_url = str(d.get("base_url", "")).rstrip("/")
        report_path = str(d.get("report_path", "")).strip()
        if not base_url:
            raise ConfigError("translations_bip.base_url is required.")
        if not report_path:
            raise ConfigError("translations_bip.report_path is required.")
        return cls(
            base_url=base_url,
            report_path=report_path,
            book_type_code=str(d.get("book_type_code", "US CORP BOOK")),
            bearer_token_env=d.get("bearer_token_env"),
            username=d.get("username"),
            password_env=d.get("password_env"),
            verify_ssl=bool(d.get("verify_ssl", True)),
            require_proxy=bool(d.get("require_proxy", False)),
            timeout_seconds=int(d.get("timeout_seconds", 120)),
        )


class BipTranslationsLoader:
    """Pulls the CMDB translations report and builds the oracle_translations dict."""

    def __init__(
        self,
        cfg: BipTranslationsConfig,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._cfg = cfg
        self._token_provider = token_provider
        self._session = requests.Session()
        self._session.proxies = get_proxy_config(require=cfg.require_proxy)
        self._session.verify = cfg.verify_ssl

    def load(self) -> dict[str, dict[str, int]]:
        """Fetch the report and return the oracle_translations mapping."""
        log.info(
            "Loading oracle_translations from BIP report %s (book=%s)",
            self._cfg.report_path,
            self._cfg.book_type_code,
        )
        rows = self._run_report()
        translations: dict[str, dict[str, int]] = {f: {} for f in _KNOWN_FIELDS}
        skipped = 0
        for row in rows:
            field = row.get("TRANSLATION_FIELD", "").strip()
            cmdb_code = row.get("CMDB_CODE", "").strip()
            raw_id = row.get("ORACLE_FA_ID", "").strip()
            if field not in _KNOWN_FIELDS or not cmdb_code or not raw_id:
                skipped += 1
                continue
            try:
                translations[field][cmdb_code] = int(raw_id)
            except ValueError:
                log.warning(
                    "BIP translations: non-integer ORACLE_FA_ID=%r for %s=%r; skipped",
                    raw_id,
                    field,
                    cmdb_code,
                )
                skipped += 1
        total = sum(len(v) for v in translations.values())
        log.info(
            "BIP translations loaded: %d entries (%d skipped). "
            "location_id=%d expense_ccid=%d employee_id=%d",
            total,
            skipped,
            len(translations["location_id"]),
            len(translations["expense_ccid"]),
            len(translations["employee_id"]),
        )
        return translations

    def _run_report(self) -> list[dict[str, str]]:
        payload = self._build_soap_envelope()
        url = f"{self._cfg.base_url}/xmlpserver/services/ExternalReportWSSService"
        log.info("BIP SOAP POST %s", url)
        resp = self._session.post(
            url,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
                **self._auth_headers(),
            },
            timeout=self._cfg.timeout_seconds,
        )
        if resp.status_code >= 400:
            raise ConfigError(
                f"BIP report HTTP {resp.status_code}: {resp.text[:400]}"
            )
        report_bytes = _extract_report_bytes(resp.content)
        return _parse_g1_rows(report_bytes)

    def _auth_headers(self) -> dict[str, str]:
        if self._token_provider:
            return {"Authorization": f"Bearer {self._token_provider()}"}
        token_env = self._cfg.bearer_token_env
        if token_env:
            token = os.environ.get(token_env, "").strip()
            if token:
                return {"Authorization": f"Bearer {token}"}
        if self._cfg.username and self._cfg.password_env:
            password = os.environ.get(self._cfg.password_env or "", "").strip()
            if password:
                encoded = base64.b64encode(
                    f"{self._cfg.username}:{password}".encode()
                ).decode("ascii")
                return {"Authorization": f"Basic {encoded}"}
        raise ConfigError(
            "translations_bip: no auth configured; set bearer_token_env or "
            "username + password_env."
        )

    def _build_soap_envelope(self) -> str:
        param_xml = (
            "<pub:parameterNameValues>"
            "<pub:item>"
            "<pub:name>P_BOOK_TYPE_CODE</pub:name>"
            f"<pub:values><pub:item>{_xml_escape(self._cfg.book_type_code)}</pub:item></pub:values>"
            "</pub:item>"
            "</pub:parameterNameValues>"
        )
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{_SOAP_NS}" xmlns:pub="{_PUB_NS}">'
            "<soap:Header/>"
            "<soap:Body>"
            "<pub:runReport>"
            "<pub:reportRequest>"
            f"{param_xml}"
            f"<pub:reportAbsolutePath>{_xml_escape(self._cfg.report_path)}</pub:reportAbsolutePath>"
            "<pub:sizeOfDataChunkDownload>-1</pub:sizeOfDataChunkDownload>"
            "<pub:reportOutputPath/>"
            "</pub:reportRequest>"
            "</pub:runReport>"
            "</soap:Body>"
            "</soap:Envelope>"
        )


def _extract_report_bytes(soap_response: bytes) -> bytes:
    root = ET.fromstring(soap_response)
    for elem in root.iter():
        local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if local == "reportBytes" and elem.text:
            return base64.b64decode(elem.text)
    raise ConfigError("No reportBytes in BIP SOAP response.")


def _parse_g1_rows(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig")
    root = ET.fromstring(text)
    rows: list[dict[str, str]] = []
    for g1 in root.findall(".//G_1"):
        row: dict[str, str] = {}
        for child in g1:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            row[local] = (child.text or "").strip()
        rows.append(row)
    return rows


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
