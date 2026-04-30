"""Real Oracle Fusion FA mass-additions client.

Replaces ``MockOracleFaClient`` for production runs.  Per Oracle's
documented FA Integration Service operations
(https://docs.oracle.com/en/cloud/saas/financials/), every read and
write goes through the **same** ``processTransaction`` REST endpoint:

  * ``processTransaction-getMassAddition``    (FA_GET_ASSET_INFO_PUB.GET_ASSET_INFO)
  * ``processTransaction-updateMassAddition`` (FA_MASS_ADD_UPD_PUB.UPDATE_MASS_ADDITION)

Discovery (which IDs are NEW for a given book) is *not* a Fusion
operation — there's no ``listMassAdditions``.  IDs come from a
:class:`~ofam_mass_additions.oracle.discovery.MassAdditionDiscovery`
implementation injected at construction (config-driven via
``run_mass_additions.py``).

Self-contained: no imports from ofam_asset_xfer.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests

from ofam_mass_additions.exceptions import FusionApiError
from ofam_mass_additions.models import MassAddition
from ofam_mass_additions.oracle.discovery import MassAdditionDiscovery
from ofam_mass_additions.oracle.paramlist import build_parameter_list
from ofam_mass_additions.proxy_config import get_proxy_config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FusionFaConfig:
    """Configuration for :class:`FusionFaClient`.

    All read/write traffic targets one URL:

        ``<base_url>/fscmRestApi/resources/<api_version>/<erp_integrations_resource>``

    with body
    ``{"OperationName": "processTransaction-<handle>", "ParameterList": "..."}``.
    """

    base_url: str
    api_version: str = "11.13.18.05"
    bearer_token_env: str = "FUSION_JWT"
    verify_ssl: bool = True
    require_proxy: bool = False
    timeout_seconds: int = 60
    erp_integrations_resource: str = "erpintegrations"

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
            erp_integrations_resource=str(
                d.get("erp_integrations_resource", "erpintegrations")
            ),
        )


class FusionFaClient:
    """Drop-in replacement for ``MockOracleFaClient`` against real Fusion.

    Authentication: pass either a ``token_provider`` callable returning a
    fresh JWT per request (the keytab/PingFed flow), or rely on
    ``cfg.bearer_token_env`` for static-bearer mode.  ``token_provider``
    wins when both are present.

    Discovery: pass an instance of
    :class:`~ofam_mass_additions.oracle.discovery.MassAdditionDiscovery`.
    Without it the client cannot list NEW IDs (Oracle has no native list
    operation).
    """

    def __init__(
        self,
        cfg: FusionFaConfig,
        discovery: MassAdditionDiscovery,
        token_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self._cfg = cfg
        self._discovery = discovery
        self._token_provider = token_provider
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

    def list_new_mass_addition_ids(self, *, book_type_code: str) -> list[str]:
        """Delegate to the configured discovery.

        See :mod:`ofam_mass_additions.oracle.discovery` for available
        modes (``explicit_ids``, ``bip_report``).
        """
        return list(self._discovery.list_new_mass_addition_ids(book_type_code=book_type_code))

    # ---- Read (processTransaction-getMassAddition) -----------------------

    def get_mass_addition(self, mass_addition_id: str) -> MassAddition:
        """Read one mass-addition row by ID."""
        params = {"P_MASS_ADDITION_ID": mass_addition_id}
        response_pl = self._process_transaction("getMassAddition", params)
        return self._parse_get_response(mass_addition_id, response_pl)

    # ---- Write (processTransaction-updateMassAddition) -------------------

    def update_mass_addition(self, payload: Dict[str, Any]) -> str:
        """Call ``processTransaction-updateMassAddition`` with ``payload``.

        Returns the raw ``ParameterList`` string from the response so the
        caller (``parse_oracle_status``) can extract ``X_RETURN_STATUS`` /
        ``X_MSG_DATA`` for audit logging.
        """
        return self._process_transaction_raw("updateMassAddition", payload)

    # ---- Internals -------------------------------------------------------

    def _erpint_url(self) -> str:
        return (
            f"{self._cfg.base_url}/fscmRestApi/resources/"
            f"{self._cfg.api_version}/{self._cfg.erp_integrations_resource}"
        )

    def _process_transaction_raw(self, handle: str, params: Dict[str, Any]) -> str:
        """POST processTransaction-<handle>; return raw ParameterList string."""
        body = {
            "OperationName": f"processTransaction-{handle}",
            "ParameterList": build_parameter_list(params),
        }
        log.debug("POST %s payload=%s", self._erpint_url(), body)
        try:
            resp = self._session.post(
                self._erpint_url(),
                json=body,
                headers=self._auth_headers(),
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise FusionApiError(
                f"POST processTransaction-{handle} failed: {exc}"
            ) from exc

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
        if pl_raw is None:
            return json.dumps(raw)
        return pl_raw if isinstance(pl_raw, str) else json.dumps(pl_raw)

    def _process_transaction(self, handle: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """POST processTransaction-<handle>; return ParameterList parsed to dict.

        Use this for *read* operations where the caller needs the X_*
        fields.  For writes, use :meth:`_process_transaction_raw` so the
        caller can decide how to interpret the response.
        """
        pl_raw = self._process_transaction_raw(handle, params)
        return _parse_parameter_list(pl_raw)

    def _auth_headers(self) -> Dict[str, str]:
        if self._token_provider is not None:
            token = self._token_provider()
        else:
            token = os.environ.get(self._cfg.bearer_token_env, "").strip()
        if not token:
            raise FusionApiError(
                "No Fusion JWT available: configure 'fusion_auth' with a keytab/"
                "password provider, or pre-populate the env var named in "
                f"'oracle.bearer_token_env' (currently {self._cfg.bearer_token_env!r})."
            )
        return {"Authorization": f"Bearer {token}"}

    # ---- Response mapping ------------------------------------------------

    def _parse_get_response(
        self, mass_addition_id: str, pl: Dict[str, Any]
    ) -> MassAddition:
        """Map an ``X_*`` response from ``getMassAddition`` to ``MassAddition``.

        Field mapping is best-effort against the documented X_* names.
        Tenants with custom DFFs may need a follow-up override (deferred
        until field naming is confirmed with the Fusion team).
        """
        status = str(pl.get("X_RETURN_STATUS", "")).strip()
        if status and status != "S":
            msg = pl.get("X_MSG_DATA") or pl.get("X_MSG_COUNT") or "(no message)"
            raise FusionApiError(
                f"getMassAddition returned X_RETURN_STATUS={status!r} for "
                f"P_MASS_ADDITION_ID={mass_addition_id}: {msg}"
            )

        return MassAddition(
            mass_addition_id=str(mass_addition_id),
            book_type_code=str(pl.get("X_BOOK_TYPE_CODE", "") or ""),
            region=str(pl.get("X_REGION", "") or ""),  # often blank; derive externally
            queue_name=str(pl.get("X_QUEUE_NAME", "NEW") or "NEW"),
            posting_status=str(pl.get("X_POSTING_STATUS", "NEW") or "NEW"),
            tag_number=_str_or_none(pl.get("X_TAG_NUMBER")),
            serial_number=_str_or_none(pl.get("X_SERIAL_NUMBER")),
            po_number=_str_or_none(pl.get("X_PO_NUMBER")),
            invoice_number=_str_or_none(pl.get("X_INVOICE_NUMBER")),
            ship_to_location=_str_or_none(pl.get("X_SHIP_TO_LOCATION")),
            delivery_location=_str_or_none(pl.get("X_DELIVERY_LOCATION")),
            invoice_quantity=_int_or_none(pl.get("X_PAYABLES_UNITS")),
            received_quantity=_int_or_none(pl.get("X_UNITS"))
            or _int_or_none(pl.get("X_PAYABLES_UNITS")),
            cost=_float_or_none(pl.get("X_COST")),
            source_system=str(pl.get("X_FEEDER_SYSTEM_NAME", "OTBI") or "OTBI"),
        )


# ============================================================================
# ParameterList parsing helpers
# ============================================================================
#
# Oracle's processTransaction response can return ``ParameterList`` in a few
# shapes depending on the operation:
#
#   1. Strict JSON with double-quoted keys/values:
#         "{\"X_RETURN_STATUS\":\"S\",\"X_MSG_COUNT\":\"0\"}"
#   2. Loose Oracle PL-format with single quotes / no quotes around keys:
#         "{X_RETURN_STATUS:'S',X_TAG_NUMBER:'TAG1',X_COST:1500.00}"
#   3. Already-parsed dict (rare, when Fusion serialises ParameterList early).
#
# ``_parse_parameter_list`` handles all three.


_PL_PAIR_RE = re.compile(
    r"""
    ([A-Z_][A-Z0-9_]*)         # KEY (uppercase identifier)
    \s* : \s*                  # colon
    (?:
        '([^']*)'              # 'quoted value'
      | ([^,'}{]+?)            # bare value: greedy run, terminated by lookahead
        \s*(?=[,}])            # followed by comma or closing brace
    )
    """,
    re.VERBOSE,
)


def _parse_parameter_list(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    s = str(raw).strip()
    if not s:
        return {}

    # First try strict JSON.
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Fall back to Oracle PL-format scanning.
    out: Dict[str, Any] = {}
    for m in _PL_PAIR_RE.finditer(s):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else m.group(3)
        out[key] = val.strip() if val is not None else None
    return out


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
