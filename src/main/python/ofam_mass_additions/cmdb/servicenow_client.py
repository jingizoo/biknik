"""ServiceNow CMDB client for asset enrichment lookups.

Replaces the in-memory ``mock_lookup.MOCK_CMDB`` table with live calls to
ServiceNow's Table API (``/api/now/table/alm_hardware``).  The schema and
auth pattern mirror the existing PL/SQL stored procedure
``CIG_LOAD_CMDB_ALM_HARDWARE_PAGES`` so the two stay in sync:

  * Same target table by default (``alm_hardware``).
  * Same field list, sourced via ``sysparm_fields`` so unused payload
    columns are not transferred.
  * Same auth precedence: Bearer token if available, else Basic API key.
  * Same proxy / SSL conventions as the rest of this repo (env-driven).

Field-name mapping is configurable so a tenant with a different CMDB
schema (e.g. ``u_fa_expense_ccid`` instead of ``cost_center.value``) can
override individual fields without forking the client.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from ofam_mass_additions.models import CmdbAsset, MassAddition
from ofam_mass_additions.proxy_config import get_proxy_config

log = logging.getLogger(__name__)


_LOOKUP_ORDER: tuple[str, ...] = ("tag_number", "serial_number", "po_number", "invoice_number")


@dataclass(frozen=True)
class ServiceNowConfig:
    """Configuration for :class:`ServiceNowCmdbClient`.

    Auth precedence: ``bearer_token_env`` first, then ``api_key_env`` as
    HTTP Basic.  At least one must be set.

    The ``*_field`` attributes name the CMDB columns that hold the
    corresponding FA values.  Override per tenant when the schema
    differs from ServiceNow's stock ``alm_hardware``.
    """

    base_url: str
    bearer_token_env: str | None = None
    api_key_env: str | None = None
    # Raw username + env var holding its password. When both are set we
    # base64-encode "user:pass" ourselves and send Basic auth — same shape
    # Postman produces from its Username/Password fields.
    username: str | None = None
    password_env: str | None = None
    table: str = "alm_hardware"
    timeout_seconds: int = 30
    verify_ssl: bool = True
    require_proxy: bool = False
    # When True the client logs the raw matched ServiceNow row at INFO,
    # so the operator can see exactly which columns came back populated.
    # Plumbed from ``run_mass_additions.py --debug-dump``.
    debug_dump: bool = False

    # CMDB column names (override when tenant uses custom fields).
    asset_tag_field: str = "asset_tag"
    serial_number_field: str = "serial_number"
    po_number_field: str | None = None  # not on alm_hardware by default
    invoice_number_field: str | None = None  # not on alm_hardware by default
    location_id_field: str = "location.u_fin_location_id"
    expense_ccid_field: str = "cost_center.value"
    employee_id_field: str = "assigned_to.u_employee_id"
    category_field: str = "model_category.name"
    ccid_active_field: str | None = None  # tenant-specific flag (e.g. "u_ccid_active")

    extra_fields: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServiceNowConfig":
        """Build a config from a JSON-friendly dict (e.g. from config.json)."""
        if "base_url" not in d:
            raise ValueError("ServiceNowConfig requires 'base_url'.")
        return cls(
            base_url=d["base_url"],
            bearer_token_env=d.get("bearer_token_env"),
            api_key_env=d.get("api_key_env"),
            username=d.get("username"),
            password_env=d.get("password_env"),
            table=d.get("table", "alm_hardware"),
            timeout_seconds=int(d.get("timeout_seconds", 30)),
            verify_ssl=bool(d.get("verify_ssl", True)),
            require_proxy=bool(d.get("require_proxy", False)),
            asset_tag_field=d.get("asset_tag_field", "asset_tag"),
            serial_number_field=d.get("serial_number_field", "serial_number"),
            po_number_field=d.get("po_number_field"),
            invoice_number_field=d.get("invoice_number_field"),
            location_id_field=d.get("location_id_field", "location.u_fin_location_id"),
            expense_ccid_field=d.get("expense_ccid_field", "cost_center.value"),
            employee_id_field=d.get("employee_id_field", "assigned_to.u_employee_id"),
            category_field=d.get("category_field", "model_category.name"),
            ccid_active_field=d.get("ccid_active_field"),
            extra_fields=tuple(d.get("extra_fields", ())),
        )


class ServiceNowCmdbClient:
    """Thin client for ServiceNow CMDB asset lookups.

    Stateless across calls (one Session, no per-asset caching).  Use
    :func:`make_servicenow_lookup` to get a function compatible with the
    ``MassAdditionCycleRunner.cmdb_lookup`` contract.
    """

    def __init__(self, cfg: ServiceNowConfig) -> None:
        self._cfg = cfg
        self._session = requests.Session()
        self._session.proxies = get_proxy_config(require=cfg.require_proxy)
        self._session.verify = cfg.verify_ssl

    def lookup(self, *, field_name: str, value: str) -> CmdbAsset | None:
        """Look up a single CMDB row by ``field_name`` and ``value``.

        ``field_name`` is a ``MassAddition`` attribute name
        (``"tag_number"``, ``"serial_number"``, …).  Returns ``None``
        when the field is unsupported by this client or the query
        returns zero rows.
        """
        cmdb_field = self._cmdb_field_for(field_name)
        if cmdb_field is None:
            log.info(
                "CMDB skip: %s -> not configured for this tenant (value=%s)",
                field_name,
                value,
            )
            return None

        url = f"{self._cfg.base_url.rstrip('/')}/api/now/table/{self._cfg.table}"
        params = {
            "sysparm_query": f"{cmdb_field}={value}",
            "sysparm_exclude_reference_link": "true",
            "sysparm_fields": ",".join(self._sysparm_fields()),
            "sysparm_limit": "1",
        }
        log.info("GET %s?sysparm_query=%s=%s", url, cmdb_field, value)

        try:
            resp = self._session.get(
                url,
                headers=self._auth_headers(),
                params=params,
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            log.warning("ServiceNow GET failed for %s=%s: %s", cmdb_field, value, exc)
            return None

        log.info(
            "ServiceNow CMDB %s=%s -> HTTP %d", cmdb_field, value, resp.status_code
        )

        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        results = (resp.json() or {}).get("result") or []
        if not results:
            log.info("ServiceNow CMDB %s=%s -> 0 rows", cmdb_field, value)
            return None
        log.info(
            "ServiceNow CMDB %s=%s -> matched sys_id=%s",
            cmdb_field,
            value,
            results[0].get("sys_id"),
        )
        if self._cfg.debug_dump:
            log.info("CMDB raw row contents: %r", results[0])
        return self._row_to_cmdb_asset(field_name, value, results[0])

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}

        bearer = self._read_env(self._cfg.bearer_token_env)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
            return headers

        if self._cfg.username and self._cfg.password_env:
            password = self._read_env(self._cfg.password_env)
            if password:
                token = base64.b64encode(
                    f"{self._cfg.username}:{password}".encode("utf-8")
                ).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
                return headers

        api_key = self._read_env(self._cfg.api_key_env)
        if api_key:
            # ServiceNow API keys are sent as HTTP Basic, base64-encoded
            # by the caller (this matches the stored proc convention,
            # which assumes API_KEY is already the encoded value).
            headers["Authorization"] = f"Basic {api_key}"
            return headers

        raise RuntimeError(
            "ServiceNow auth not configured: set bearer_token_env, "
            "username + password_env, or api_key_env."
        )

    @staticmethod
    def _read_env(var: str | None) -> str | None:
        if not var:
            return None
        value = os.environ.get(var)
        return value.strip() if value else None

    def _cmdb_field_for(self, mass_addition_field: str) -> str | None:
        return {
            "tag_number": self._cfg.asset_tag_field,
            "serial_number": self._cfg.serial_number_field,
            "po_number": self._cfg.po_number_field,
            "invoice_number": self._cfg.invoice_number_field,
        }.get(mass_addition_field)

    def _sysparm_fields(self) -> list[str]:
        # Always include sys_id + every field we map into CmdbAsset.
        # Optional fields (employee_id_field, category_field, etc.) may be
        # configured as null in JSON; skip those.
        candidates = [
            "sys_id",
            self._cfg.asset_tag_field,
            self._cfg.serial_number_field,
            self._cfg.location_id_field,
            self._cfg.expense_ccid_field,
            self._cfg.employee_id_field,
            self._cfg.category_field,
        ]
        names = [n for n in candidates if n]
        if self._cfg.ccid_active_field:
            names.append(self._cfg.ccid_active_field)
        names.extend(self._cfg.extra_fields)
        # de-dup while preserving order
        seen: set[str] = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def _row_to_cmdb_asset(
        self, source_key: str, source_value: str, row: dict[str, Any]
    ) -> CmdbAsset:
        ccid_active = True
        if self._cfg.ccid_active_field:
            raw = row.get(self._cfg.ccid_active_field)
            ccid_active = _to_bool(raw, default=True)

        # Use _to_id (int when parseable, otherwise the trimmed string).
        # The deeper SNOW paths return codes like "LC000238" or 32-char
        # sys_ids — both are strings, both must flow through.
        emp_field = self._cfg.employee_id_field
        return CmdbAsset(
            source_key=source_key,
            source_value=source_value,
            location_id=_to_id(row.get(self._cfg.location_id_field)),
            expense_ccid=_to_id(row.get(self._cfg.expense_ccid_field)),
            employee_id=_to_id(row.get(emp_field)) if emp_field else None,
            ccid_active=ccid_active,
        )


def make_servicenow_lookup(
    cfg: ServiceNowConfig,
) -> Callable[[MassAddition], CmdbAsset | None]:
    """Build a lookup function that walks the standard field order.

    The returned callable matches the contract used by
    :class:`ofam_mass_additions.runner.cycle.MassAdditionCycleRunner`'s
    ``cmdb_lookup`` parameter.
    """
    client = ServiceNowCmdbClient(cfg)

    def _lookup(mass_addition: MassAddition) -> CmdbAsset | None:
        log.info(
            "CMDB lookup for mass_addition_id=%s "
            "(tag=%s serial=%s po=%s invoice=%s)",
            mass_addition.mass_addition_id,
            mass_addition.tag_number,
            mass_addition.serial_number,
            mass_addition.po_number,
            mass_addition.invoice_number,
        )
        for field_name in _LOOKUP_ORDER:
            value = getattr(mass_addition, field_name, None)
            if not value:
                continue
            hit = client.lookup(field_name=field_name, value=value)
            if hit is not None:
                log.info(
                    "CMDB hit for mass_addition_id=%s via %s=%s",
                    mass_addition.mass_addition_id,
                    field_name,
                    value,
                )
                return hit
        log.info(
            "CMDB miss for mass_addition_id=%s (no match across tag/serial/po/invoice)",
            mass_addition.mass_addition_id,
        )
        return None

    return _lookup


def _to_int(value: Any) -> int | None:
    """Coerce a CMDB string value to int; return None on empty / non-numeric."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _to_id(value: Any) -> int | str | None:
    """Coerce to int when parseable, otherwise return the trimmed string.

    CMDB returns a mix of shapes:
      * numeric strings ("682610")               -> int 682610
      * zero-padded codes ("00000017")           -> str "00000017"  (preserve padding)
      * code strings ("LC000238")                -> str "LC000238"
      * 32-char sys_ids ("a75acddd...")          -> str "a75acddd..."
      * empty / missing                          -> None

    Leading zeros are preserved as strings — int() would silently drop
    them and DFF segments (e.g. ``X_CAT_ATTRIBUTE1: '00000000017'``)
    use that padding as part of the identifier.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return None
    if len(s) > 1 and s.startswith("0"):
        return s
    try:
        return int(s)
    except ValueError:
        return s


def _to_bool(value: Any, *, default: bool) -> bool:
    """Coerce ServiceNow's typical Y/N | true/false strings to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "y", "yes", "1"):
        return True
    if s in ("false", "n", "no", "0"):
        return False
    return default
