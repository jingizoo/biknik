"""Oracle Fusion ERP Integrations REST client for asset transactions."""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

import httpx

from .exceptions import FusionApiError
from .paramlist import build_parameter_list
from .proxy_config import get_proxy_config

log = logging.getLogger(__name__)

# Headers that may contain secrets; values are replaced with a marker
# before being written to disk.
_SECRET_HEADER_NAMES = frozenset(
    {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
)
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _parse_fusion_response(r: "httpx.Response", context: str) -> Dict[str, Any]:
    """Parse a Fusion REST response, raising FusionApiError on any failure.

    The Fusion gateway can return empty bodies or HTML error pages on
    auth, proxy, or upstream failures.  Parsing JSON before checking the
    HTTP status surfaces a generic ``JSONDecodeError`` and hides the real
    cause (e.g. ``HTTP 401`` or ``HTTP 502``).  This helper inverts that
    order: status first, body shape second, JSON parse last.
    """
    status = r.status_code
    body = r.text or ""
    snippet = body[:500].strip() or "(empty body)"

    if status >= 400:
        raise FusionApiError(f"Fusion {context} HTTP {status}: {snippet}")

    if not body.strip():
        raise FusionApiError(
            f"Fusion {context} returned empty body (status={status}); "
            "expected JSON. Possible upstream timeout or proxy failure."
        )

    try:
        parsed = r.json()
    except ValueError as e:
        raise FusionApiError(
            f"Fusion {context} returned non-JSON response " f"(status={status}): {snippet}"
        ) from e

    if not isinstance(parsed, dict):
        raise FusionApiError(
            f"Fusion {context} returned unexpected JSON shape "
            f"(status={status}, type={type(parsed).__name__}): {snippet}"
        )
    return parsed


def _redact_headers(headers: Any) -> Dict[str, str]:
    """Return a copy of ``headers`` with secret values masked."""
    out: Dict[str, str] = {}
    try:
        items = headers.items()  # CaseInsensitiveDict / dict
    except AttributeError:
        return {}
    for k, v in items:
        if str(k).lower() in _SECRET_HEADER_NAMES:
            out[str(k)] = "***REDACTED***"
        else:
            out[str(k)] = str(v)
    return out


class _ExchangeDumper:
    """Writes Fusion request/response pairs to per-call JSON files.

    Enabled when ``--debug`` is passed at the CLI: every POST/GET that
    flows through the client is captured to ``<dir>/NNNN-<ts>-<tag>.json``
    so an operator can inspect the exact wire payloads after the fact.
    Authorization and other secret-bearing headers are redacted before
    being written.  Failures to write a dump are logged but never raised
    — debugging output must not break the live transfer.
    """

    def __init__(self, directory: Union[str, Path]) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        log.info("Fusion debug dumps will be written to %s", self._dir)

    def dump(
        self,
        *,
        method: str,
        url: str,
        request_headers: Any,
        request_body: Any,
        response: Optional["httpx.Response"],
        error: Optional[str],
        elapsed_ms: float,
        tag: str,
    ) -> None:
        try:
            with self._lock:
                seq = next(self._counter)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            slug = _FILENAME_SAFE.sub("_", tag)[:80] or "call"
            path = self._dir / f"{seq:04d}-{ts}-{method}-{slug}.json"

            payload: Dict[str, Any] = {
                "sequence": seq,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": round(elapsed_ms, 2),
                "request": {
                    "method": method,
                    "url": url,
                    "headers": _redact_headers(request_headers),
                    "body": request_body,
                },
            }
            if response is not None:
                resp_text = response.text or ""
                resp_body: Any = resp_text
                # Best-effort JSON parse so the dump is easier to read,
                # but always preserve the raw text on failure.
                try:
                    resp_body = response.json()
                except ValueError:
                    pass
                payload["response"] = {
                    "status_code": response.status_code,
                    "headers": _redact_headers(response.headers),
                    "body": resp_body,
                    "raw_text": resp_text if resp_body is not resp_text else None,
                }
            if error is not None:
                payload["error"] = error

            path.write_text(json.dumps(payload, indent=2, default=str))
        except Exception:  # never let dumping break the call
            log.warning("Failed to write Fusion debug dump", exc_info=True)


@dataclass(frozen=True)
class OracleConfig:
    """Immutable config for :class:`OracleErpIntegrationsClient`."""

    base_url: str
    api_version: str
    bearer_token: str = ""  # may be empty when using token_provider
    verify_ssl: bool = True
    timeout_seconds: int = 60
    require_proxy: bool = False

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OracleConfig":
        """Build an OracleConfig from a raw config dictionary.

        The ``bearer_token`` may be left empty when a ``token_provider`` is
        passed to :class:`OracleErpIntegrationsClient`.  The provider then
        supplies a fresh token per request.
        """
        base_url = str(d.get("base_url", "")).rstrip("/")
        api_version = str(d.get("api_version", "")).strip()
        if not base_url or not api_version:
            raise ValueError("oracle.base_url and oracle.api_version are required")

        # Bearer token: sourced from config or, preferably, from an env var
        # injected by CDX secrets at pod startup (bearer_token_env points to
        # the env var name, e.g. "FUSION_JWT").  In production the token is
        # rotated by the CDX secrets sidecar; for local dev it can be set
        # directly in the config as "bearer_token".
        #
        # When a FusionTokenProvider is used (top-level fusion_auth block),
        # no token is required here — the provider supplies one per request.
        bearer_token = d.get("bearer_token", "")
        bearer_token_env = d.get("bearer_token_env")

        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        return OracleConfig(
            base_url=base_url,
            api_version=api_version,
            bearer_token=str(bearer_token or ""),
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
            require_proxy=bool(d.get("require_proxy", False)),
        )


class OracleErpIntegrationsClient:
    """Client for Oracle Fusion ERP Integration REST Service for Assets transactions.

    Authorization header is set **per request** so that a refreshing
    ``token_provider`` (keytab -> PingFed -> JWT service) always supplies a
    currently-valid JWT.  If no provider is given, the static token from
    ``OracleConfig.bearer_token`` is used.
    """

    def __init__(
        self,
        cfg: OracleConfig,
        token_provider: Optional[Callable[[], str]] = None,
        debug_dir: Union[str, Path, None] = None,
    ):
        """Initialise the client with config and optional token provider.

        ``debug_dir``: when set, every Fusion request/response pair is
        written to a JSON file under that directory.  Plumbed from the
        ``--debug`` CLI flag.  Auth and other secret-bearing headers are
        redacted before being written.
        """
        self.cfg = cfg
        self._token_provider = token_provider

        if not token_provider and not cfg.bearer_token:
            raise ValueError(
                "OracleErpIntegrationsClient requires either a token_provider "
                "or OracleConfig.bearer_token"
            )

        self._session = httpx.Client(
            trust_env=not cfg.require_proxy,
            headers={
                "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
                "REST-header-version": "4",
                "ACCEPT": "application/json",
            },
            proxies=get_proxy_config(require=cfg.require_proxy),
        )

        self._dumper = _ExchangeDumper(debug_dir) if debug_dir else None

    def _auth_headers(self) -> Dict[str, str]:
        """Build fresh Authorization header (uses provider if supplied)."""
        token = self._token_provider() if self._token_provider else self.cfg.bearer_token
        return {"Authorization": f"Bearer {token}"}

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

        auth_headers = self._auth_headers()
        request_headers = {**self._session.headers, **auth_headers}
        r: Optional["httpx.Response"] = None
        transport_error: Optional[str] = None
        started = datetime.now(timezone.utc)
        try:
            r = self._session.post(
                url,
                json=payload,
                headers=auth_headers,
                timeout=self.cfg.timeout_seconds,
                verify=self.cfg.verify_ssl,
            )
        except Exception as e:
            transport_error = f"{type(e).__name__}: {e}"
            self._maybe_dump(
                method="POST",
                url=url,
                request_headers=request_headers,
                request_body=payload,
                response=None,
                error=transport_error,
                started=started,
                tag=op_name,
            )
            raise
        self._maybe_dump(
            method="POST",
            url=url,
            request_headers=request_headers,
            request_body=payload,
            response=r,
            error=None,
            started=started,
            tag=op_name,
        )
        raw = _parse_fusion_response(r, context=f"POST {op_name}")

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

        auth_headers = self._auth_headers()
        request_headers = {**self._session.headers, **auth_headers}
        request_body = {"query_params": dict(query_params or {})}
        r: Optional["httpx.Response"] = None
        transport_error: Optional[str] = None
        started = datetime.now(timezone.utc)
        try:
            r = self._session.get(
                url,
                params=query_params or {},
                headers=auth_headers,
                timeout=self.cfg.timeout_seconds,
                verify=self.cfg.verify_ssl,
            )
        except Exception as e:
            transport_error = f"{type(e).__name__}: {e}"
            self._maybe_dump(
                method="GET",
                url=url,
                request_headers=request_headers,
                request_body=request_body,
                response=None,
                error=transport_error,
                started=started,
                tag=resource_path,
            )
            raise
        self._maybe_dump(
            method="GET",
            url=url,
            request_headers=request_headers,
            request_body=request_body,
            response=r,
            error=None,
            started=started,
            tag=resource_path,
        )
        raw = _parse_fusion_response(r, context=f"GET {resource_path}")
        return dict(raw)

    def _maybe_dump(
        self,
        *,
        method: str,
        url: str,
        request_headers: Dict[str, Any],
        request_body: Any,
        response: Optional["httpx.Response"],
        error: Optional[str],
        started: datetime,
        tag: str,
    ) -> None:
        if self._dumper is None:
            return
        elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
        self._dumper.dump(
            method=method,
            url=url,
            request_headers=request_headers,
            request_body=request_body,
            response=response,
            error=error,
            elapsed_ms=elapsed_ms,
            tag=tag,
        )
