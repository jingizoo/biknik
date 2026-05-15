"""Oracle Fusion GL AccountCombinationService SOAP client.

When the OFAM cross-book CCID resolver can't find a destination
account combination (e.g. the target ledger has the right Company
segment but no row exists yet for that segment combo), this client
mints a new one via SOAP::

    POST {base_url}/fscmService/AccountCombinationService
    Body: validateAndCreateAccounts(validationInputRowList=[...])

with ``DynamicInsertion=Yes``.  Oracle validates the segment combo
against cross-validation rules, segment value security, dates, etc.
and either returns the existing/new ``CcId`` or an ``Error`` message.

Auth uses the same Bearer JWT as the rest of the pipeline (token
provider returns a freshly-rotated token per call).

Only the synchronous ``validateAndCreateAccounts`` operation is
implemented.  ``validateAccounts`` (validation-only, no create) and
asynchronous-batch variants are out of scope for now; add them when a
caller needs them.

Required Fusion privilege:
``GL_MANAGE_GENERAL_LEDGER_ACCOUNT_COMBINATIONS_PRIV``
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional
from xml.etree import ElementTree as ET

import requests  # type: ignore[import-untyped]

from .exceptions import FusionApiError
from .proxy_config import get_proxy_config

log = logging.getLogger(__name__)

# Oracle's AccountCombinationService namespaces.  Picked from the WSDL:
# https://<host>/fscmService/AccountCombinationService?WSDL
_TYPES_NS = (
    "http://xmlns.oracle.com/apps/financials/generalLedger/accounts/"
    "codeCombinations/accountCombinationService/types/"
)
_ACC_NS = (
    "http://xmlns.oracle.com/apps/financials/generalLedger/accounts/"
    "codeCombinations/accountCombinationService/"
)
# SOAP 1.1 — AccountCombinationService is SOAP 1.1, unlike BIP which is 1.2.
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


def _xml_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@dataclass(frozen=True)
class AccountCombinationServiceConfig:
    """Configuration for :class:`AccountCombinationServiceClient`."""

    base_url: str
    endpoint_path: str = "/fscmService/AccountCombinationService"
    bearer_token: str = ""  # may be empty when using token_provider
    verify_ssl: bool = True
    timeout_seconds: int = 60
    require_proxy: bool = False

    # Defaults applied to every validateAndCreateAccounts call when not
    # provided by the caller.  Override per-call by passing the matching
    # kwarg to ``validate_and_create``.
    #
    # The documented Citadel SOAP template includes only ``DynamicInsertion``
    # + ``Segment*`` + ``LedgerName``; ``EnabledFlag`` and ``FromDate`` are
    # schema-optional and not in the canonical envelope.  We leave them
    # OFF by default and only include them when explicitly configured —
    # set ``"enabled_flag": "true"`` / ``"from_date": "2026-05-14"`` to
    # opt in.
    dynamic_insertion: str = "Yes"
    enabled_flag: Optional[str] = None
    from_date: Optional[str] = None

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "AccountCombinationServiceConfig":
        """Build a config from a JSON-friendly dict."""
        base_url = str(d.get("base_url", "")).rstrip("/")
        if not base_url:
            raise ValueError("account_combination_service.base_url is required")

        bearer_token = d.get("bearer_token", "")
        bearer_token_env = d.get("bearer_token_env")
        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        ef = d.get("enabled_flag")
        fd = d.get("from_date")
        return AccountCombinationServiceConfig(
            base_url=base_url,
            endpoint_path=str(
                d.get("endpoint_path") or "/fscmService/AccountCombinationService"
            ).strip(),
            bearer_token=str(bearer_token or ""),
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            require_proxy=bool(d.get("require_proxy", False)),
            dynamic_insertion=str(d.get("dynamic_insertion", "Yes")),
            enabled_flag=None if ef is None or str(ef).strip() == "" else str(ef),
            from_date=None if fd is None or str(fd).strip() == "" else str(fd),
        )


@dataclass(frozen=True)
class CreatedCombination:
    """Outcome of one ``validateAndCreateAccounts`` row."""

    ccid: Optional[int]
    status: str  # e.g. "S" (success) or "E" (error)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    concatenated_segments: Optional[str] = None


class AccountCombinationServiceClient:
    """SOAP client for ``validateAndCreateAccounts``.

    Mirrors the auth + proxy + session pattern used by
    :class:`BIPClient` and :class:`OracleErpIntegrationsClient` so the
    same Bearer token provider feeds all three.
    """

    def __init__(
        self,
        cfg: AccountCombinationServiceConfig,
        token_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        """Initialise the client.

        ``token_provider`` is preferred over ``cfg.bearer_token`` so a
        keytab-backed JWT can be refreshed per call.
        """
        self.cfg = cfg
        self._token_provider = token_provider

        if not token_provider and not cfg.bearer_token:
            raise ValueError(
                "AccountCombinationServiceClient requires either a token_provider "
                "or AccountCombinationServiceConfig.bearer_token"
            )

        self._session = requests.Session()
        self._session.trust_env = not cfg.require_proxy
        self._session.headers.update({"Content-Type": "text/xml; charset=utf-8"})  # SOAP 1.1
        self._session.proxies.update(get_proxy_config(require=cfg.require_proxy))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def validate_and_create(
        self,
        *,
        ledger_name: str,
        segments: Mapping[str, str],
        from_date: Optional[str] = None,
        dynamic_insertion: Optional[str] = None,
        enabled_flag: Optional[str] = None,
    ) -> CreatedCombination:
        """Validate and (when ``DynamicInsertion=Yes``) create a CCID.

        Args:
            ledger_name: Target ledger that owns the chart of accounts.
            segments: Segment values keyed by ``Segment1``..``Segment30``.
            from_date: Effective date for the combination (default today).
            dynamic_insertion: Override the config default (``"Yes"``).
                Pass ``"No"`` for a pure validation call.
            enabled_flag: Override the config default (``"true"``).

        Returns:
            CreatedCombination with the new (or existing) ccid plus
            status + error info.

        Raises:
            FusionApiError on transport-level failure or malformed
            response.  Business errors (cross-validation rule violation,
            disabled segment, etc.) are returned in the ``status`` /
            ``error_message`` fields of the result, NOT raised.
        """
        if not ledger_name or not ledger_name.strip():
            raise ValueError("ledger_name is required")
        if not segments:
            raise ValueError("segments must be a non-empty mapping")

        # Optional fields: only present in the envelope when explicitly
        # supplied by the caller or set on the config.  Citadel's
        # documented template omits both EnabledFlag and FromDate;
        # auto-defaulting them caused Oracle to reject the envelope in
        # some pods, so we keep them off unless explicitly opted in.
        effective_from_date = from_date if from_date is not None else self.cfg.from_date
        effective_enabled_flag = enabled_flag if enabled_flag is not None else self.cfg.enabled_flag
        body = self._build_envelope(
            ledger_name=ledger_name.strip(),
            segments=dict(segments),
            from_date=effective_from_date,
            dynamic_insertion=dynamic_insertion or self.cfg.dynamic_insertion,
            enabled_flag=effective_enabled_flag,
        )

        url = self.cfg.base_url + self.cfg.endpoint_path
        log.debug("POST %s body=%s", url, body)

        headers = {
            "SOAPAction": "validateAndCreateAccounts",
            **self._auth_headers(),
        }
        try:
            r = self._session.post(
                url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.cfg.timeout_seconds,
                verify=self.cfg.verify_ssl,
            )
        except requests.RequestException as e:
            raise FusionApiError(f"AccountCombinationService transport error ({url}): {e}") from e

        if r.status_code >= 400:
            raise FusionApiError(
                f"AccountCombinationService HTTP {r.status_code}: " f"{(r.text or '')[:500]}"
            )

        body_text = r.text or ""
        if not body_text.strip():
            raise FusionApiError(
                f"AccountCombinationService returned empty body (status={r.status_code})"
            )

        return self._parse_response(body_text)

    # ------------------------------------------------------------------
    # Envelope build
    # ------------------------------------------------------------------
    def _build_envelope(
        self,
        *,
        ledger_name: str,
        segments: Dict[str, str],
        from_date: Optional[str],
        dynamic_insertion: str,
        enabled_flag: Optional[str],
    ) -> str:
        """Render the SOAP envelope in the order Citadel's template uses.

        The documented row order is::

            <typ:validationInputRowList>
              <acc:DynamicInsertion>...</acc:DynamicInsertion>
              <acc:Segment1>...</acc:Segment1>
              ...
              <acc:SegmentN>...</acc:SegmentN>
              <acc:LedgerName>...</acc:LedgerName>
              <!-- EnabledFlag and FromDate are also optional;
                   emitted only when the caller / config supplies them -->
            </typ:validationInputRowList>

        Most XML-schema validators accept any element order within the
        row, but Oracle's tooling occasionally rejects unexpected layouts
        so we mirror the documented template exactly.
        """
        seg_xml_parts: List[str] = []
        # Order Segment1..SegmentN numerically.
        for key in sorted(segments.keys(), key=_segment_sort_key):
            seg_xml_parts.append(f"        <acc:{key}>{_xml_escape(segments[key])}</acc:{key}>")
        segments_xml = "\n".join(seg_xml_parts)

        optional_tail_parts: List[str] = []
        if enabled_flag is not None and str(enabled_flag).strip():
            optional_tail_parts.append(
                f"<acc:EnabledFlag>{_xml_escape(enabled_flag)}</acc:EnabledFlag>"
            )
        if from_date is not None and str(from_date).strip():
            optional_tail_parts.append(f"<acc:FromDate>{_xml_escape(from_date)}</acc:FromDate>")
        optional_tail = "".join(optional_tail_parts)

        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<soapenv:Envelope xmlns:soapenv="{_SOAP_NS}" '
            f'xmlns:typ="{_TYPES_NS}" xmlns:acc="{_ACC_NS}">'
            "<soapenv:Header/>"
            "<soapenv:Body>"
            "<typ:validateAndCreateAccounts>"
            "<typ:validationInputRowList>"
            f"<acc:DynamicInsertion>{_xml_escape(dynamic_insertion)}</acc:DynamicInsertion>"
            f"{segments_xml}"
            f"<acc:LedgerName>{_xml_escape(ledger_name)}</acc:LedgerName>"
            f"{optional_tail}"
            "</typ:validationInputRowList>"
            "</typ:validateAndCreateAccounts>"
            "</soapenv:Body>"
            "</soapenv:Envelope>"
        )

    # ------------------------------------------------------------------
    # Response parse
    # ------------------------------------------------------------------
    def _parse_response(self, body: str) -> CreatedCombination:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise FusionApiError(
                f"AccountCombinationService returned non-XML response: " f"{body[:500]}"
            ) from e

        # Surface SOAP faults explicitly — Oracle wraps validation errors
        # at the application level (under ValidatedAccountCombinations)
        # so a Fault here is a transport/auth/server problem.
        fault = root.find(f"{{{_SOAP_NS}}}Body/{{{_SOAP_NS}}}Fault")
        if fault is not None:
            reason = fault.findtext("faultstring") or _innertext(fault)
            raise FusionApiError(f"AccountCombinationService SOAP fault: {reason}")

        # The result element is namespaced under the *types* NS.  Look
        # for any <result> child of the validateAndCreateAccountsResponse
        # body.  Oracle returns one <result> per input row.
        result_el = _find_first(
            root,
            (
                f"{{{_SOAP_NS}}}Body",
                None,  # validateAndCreateAccountsResponse (types ns) — match any
                None,  # result
            ),
        )
        if result_el is None:
            raise FusionApiError(
                f"AccountCombinationService response missing result element: " f"{body[:500]}"
            )

        ccid_raw = _innertext(_find_child_local(result_el, "CcId"))
        status = _innertext(_find_child_local(result_el, "Status")) or ""
        error_code = _innertext(_find_child_local(result_el, "ErrorCode"))
        error_msg = _innertext(_find_child_local(result_el, "Error"))
        concat = _innertext(_find_child_local(result_el, "ConcatenatedSegments"))

        ccid: Optional[int] = None
        if ccid_raw and ccid_raw.strip().lstrip("-").isdigit():
            ccid = int(ccid_raw.strip())

        return CreatedCombination(
            ccid=ccid,
            status=status.strip(),
            error_code=(error_code or "").strip() or None,
            error_message=(error_msg or "").strip() or None,
            concatenated_segments=(concat or "").strip() or None,
        )

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------
    def _auth_headers(self) -> Dict[str, str]:
        token = self._token_provider() if self._token_provider else self.cfg.bearer_token
        return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# XML helpers (local to this module)
# ---------------------------------------------------------------------------
def _segment_sort_key(key: str) -> tuple:
    """Sort Segment1, Segment2, ..., Segment10 numerically."""
    if key.startswith("Segment"):
        rest = key[len("Segment") :]
        if rest.isdigit():
            return (0, int(rest))
    return (1, key)


def _innertext(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    return "".join(el.itertext())


def _find_first(root: ET.Element, path: tuple) -> Optional[ET.Element]:
    """Walk ``path`` from ``root``; each step can be a tag (str) or None
    to match any child at that level.  Returns the first leaf reached
    or ``None``."""
    nodes: List[ET.Element] = [root]
    for step in path:
        next_nodes: List[ET.Element] = []
        for n in nodes:
            for child in list(n):
                if step is None or child.tag == step:
                    next_nodes.append(child)
        nodes = next_nodes
        if not nodes:
            return None
    return nodes[0] if nodes else None


def _find_child_local(parent: ET.Element, local_name: str) -> Optional[ET.Element]:
    """Find the first child whose *local* name (after the ``{ns}``
    prefix) matches ``local_name``.  Namespaces vary across Oracle
    versions so we match by local-name only."""
    for child in list(parent):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == local_name:
            return child
    return None
