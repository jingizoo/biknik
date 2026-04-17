"""Fusion JWT token provider — keytab -> PingFed SPNEGO -> ERPSecurity/CreateJWT.

Follows the Citadel ``erp_auth_helper.py`` pattern:

    keytab file (mounted from vault)
          |
          v  [gssapi credentials from keytab]
    Kerberos TGT
          |
          v  [SPNEGO negotiate via httpx_gssapi]
    PingFed access_token
          |
          v  [POST to acctgateway ERPSecurity/CreateJWT]
    Oracle Fusion JWT  (cached until ~5 min before expiry)

Environment is resolved from ``CITADEL_ENV`` env var (stabledev, test,
dev1–4, prod) and drives which SSO host and ERPSecurity endpoint is used.

Usage::

    provider = build_token_provider(config["fusion_auth"])
    oracle_client = OracleErpIntegrationsClient(cfg, token_provider=provider.get_token)
    bip_client    = BIPClient(bip_cfg,            token_provider=provider.get_token)

Configuration
-------------
Two modes — pick one via ``fusion_auth.mode``::

    # Static JWT (dev / workstation) — same as legacy bearer_token_env
    "fusion_auth": {
        "mode": "static",
        "token_env": "FUSION_JWT"
    }

    # Keytab flow (production / batch pod)
    "fusion_auth": {
        "mode": "keytab",
        "keytab_path_env": "FUSION_KEYTAB_PATH",
        "principal_env": "FUSION_PRINCIPAL",
        "ora_env": "STABLEDEV",
        "cache_buffer_seconds": 300
    }

If ``fusion_auth`` is absent the entrypoint falls back to building the clients
from the legacy ``oracle.bearer_token_env`` / ``bip.bearer_token_env`` fields
(unchanged behaviour).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .exceptions import ConfigError

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Citadel URL Maps (from erp_auth_helper.py)
# ---------------------------------------------------------------------------
SSO_HOST_MAP: Dict[str, str] = {
    "PROD": "sso.citadelgroup.com",
    "DEV": "sso-dev.citadelgroup.com",
    "STABLEDEV": "sso-dev.citadelgroup.com",
    "TEST": "sso-dev.citadelgroup.com",
    "DEV1": "sso-dev.citadelgroup.com",
    "DEV2": "sso-dev.citadelgroup.com",
    "DEV3": "sso-dev.citadelgroup.com",
    "DEV4": "sso-dev.citadelgroup.com",
}

ERP_AUTH_URL_MAP: Dict[str, str] = {
    "TEST": "https://acctgateway-erpsecurity-dev.citadelgroup.com/ERPSecurity/CreateJWT",
    "DEV1": "https://acctgateway-erpsecurity-dev1.citadelgroup.com/ERPSecurity/CreateJWT",
    "DEV2": "https://acctgateway-erpsecurity-dev2.citadelgroup.com/ERPSecurity/CreateJWT",
    "DEV3": "https://acctgateway-erpsecurity-dev3.citadelgroup.com/ERPSecurity/CreateJWT",
    "DEV4": "https://acctgateway-erpsecurity-dev4.citadelgroup.com/ERPSecurity/CreateJWT",
    "STABLEDEV": "https://acctgateway-erpsecurity-dev.citadelgroup.com/ERPSecurity/CreateJWT",
    "PROD": "https://acctgateway-erpsecurity-prod.citadelgroup.com/ERPSecurity/CreateJWT",
}


def _resolve_citadel_env() -> str:
    """Resolve Citadel environment from CITADEL_ENV (default stabledev)."""
    env = os.environ.get("CITADEL_ENV", "stabledev").strip().lower()
    # erp_auth_helper maps "dev" -> "stabledev"
    if env == "dev":
        env = "stabledev"
    return env.upper()


def _get_erp_ssl_context() -> ssl.SSLContext:
    """Custom SSL context matching erp_auth_helper.py for REALM3 compat."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers(
        "AES256-SHA:DHE-RSA-AES256-SHA:AES128-SHA:DHE-RSA-AES128-SHA"
    )
    return ctx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenProviderConfig:
    """Configuration for :class:`FusionTokenProvider`."""

    mode: str  # "static" or "keytab"

    # --- static mode -----------------------------------------------------
    static_token: str = ""

    # --- keytab mode -----------------------------------------------------
    keytab_path: str = ""
    principal: str = ""
    ora_env: str = ""  # e.g. "STABLEDEV", "DEV1", "PROD"

    # Optional overrides (if not provided, resolved from ora_env + maps)
    negotiate_url: str = ""
    erp_auth_url: str = ""

    # --- common ----------------------------------------------------------
    cache_buffer_seconds: int = 300  # refresh 5 min before expiry
    timeout_seconds: int = 30

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "TokenProviderConfig":
        """Build a TokenProviderConfig from a raw config dictionary."""
        mode = str(d.get("mode", "static")).strip().lower()
        if mode not in ("static", "keytab"):
            raise ConfigError(
                f"fusion_auth.mode must be 'static' or 'keytab' (got {mode!r})"
            )

        if mode == "static":
            token = d.get("token", "")
            token_env = d.get("token_env") or d.get("bearer_token_env")
            if token_env:
                token = os.environ.get(str(token_env), token)
            if not token:
                raise ConfigError(
                    "fusion_auth.token or fusion_auth.token_env is required "
                    "for mode=static"
                )
            return TokenProviderConfig(mode="static", static_token=str(token))

        # --- keytab mode ---
        keytab = d.get("keytab_path", "").strip()
        if not keytab:
            env_key = d.get("keytab_path_env", "").strip()
            if env_key:
                keytab = os.environ.get(env_key, "").strip()
        if not keytab:
            raise ConfigError(
                "fusion_auth.keytab_path or keytab_path_env is required "
                "for mode=keytab"
            )

        principal = d.get("principal", "").strip()
        if not principal:
            env_key = d.get("principal_env", "").strip()
            if env_key:
                principal = os.environ.get(env_key, "").strip()
        if not principal:
            raise ConfigError(
                "fusion_auth.principal or principal_env is required "
                "for mode=keytab"
            )

        # Oracle environment (drives SSO + ERP URL resolution)
        ora_env = str(
            d.get("ora_env", "")
            or os.environ.get("CITADEL_ENV", "stabledev")
        ).strip().upper()
        if ora_env == "DEV":
            ora_env = "STABLEDEV"

        # Allow explicit URL overrides; otherwise resolve from maps
        negotiate_url = str(d.get("negotiate_url", "")).strip()
        erp_auth_url = str(
            d.get("erp_auth_url", "")
            or os.environ.get("ERP_AUTH_URL", "")
        ).strip()

        if not negotiate_url:
            sso_host = SSO_HOST_MAP.get(ora_env)
            if not sso_host:
                raise ConfigError(
                    f"Unknown ora_env={ora_env!r}; set negotiate_url explicitly "
                    f"or use one of: {list(SSO_HOST_MAP.keys())}"
                )
            negotiate_url = f"https://{sso_host}/negotiate"

        if not erp_auth_url:
            erp_auth_url_resolved = ERP_AUTH_URL_MAP.get(ora_env)
            if not erp_auth_url_resolved:
                raise ConfigError(
                    f"Unknown ora_env={ora_env!r}; set erp_auth_url explicitly "
                    f"or use one of: {list(ERP_AUTH_URL_MAP.keys())}"
                )
            erp_auth_url = erp_auth_url_resolved

        return TokenProviderConfig(
            mode="keytab",
            keytab_path=keytab,
            principal=principal,
            ora_env=ora_env,
            negotiate_url=negotiate_url,
            erp_auth_url=erp_auth_url,
            cache_buffer_seconds=int(d.get("cache_buffer_seconds", 300)),
            timeout_seconds=int(d.get("timeout_seconds", 30)),
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class FusionTokenProvider:
    """Caches + refreshes Oracle Fusion JWT via keytab -> PingFed -> CreateJWT.

    Follows the Citadel ``erp_auth_helper.py`` pattern:

    1. Acquire Kerberos credentials from keytab (no external ``kinit``).
    2. SPNEGO-negotiate against ``sso[-dev].citadelgroup.com/negotiate``
       to obtain a PingFed access token.
    3. POST the PF token to ``acctgateway-erpsecurity-*.citadelgroup.com
       /ERPSecurity/CreateJWT`` with empty JSON body to get an Oracle JWT.
    4. Cache the JWT and refresh ~5 min before its ``exp`` claim.

    Thread-safe: a single refresh is serialised with a lock so concurrent
    callers can't stampede the SSO / ERP endpoints.
    """

    def __init__(self, cfg: TokenProviderConfig):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """Return a currently-valid Fusion JWT, refreshing if near expiry."""
        if self._cfg.mode == "static":
            return self._cfg.static_token

        now = time.time()
        # Fast path: valid cached token
        if (
            self._cached_token
            and now < (self._expires_at - self._cfg.cache_buffer_seconds)
        ):
            return self._cached_token

        # Slow path: refresh under lock
        with self._lock:
            now = time.time()
            if (
                self._cached_token
                and now < (self._expires_at - self._cfg.cache_buffer_seconds)
            ):
                return self._cached_token

            log.info(
                "Refreshing Fusion JWT (env=%s) via keytab -> SPNEGO -> CreateJWT",
                self._cfg.ora_env,
            )
            pf_token = self._negotiate_pingfed_token()
            fusion_jwt, expires_at = self._exchange_for_oracle_jwt(pf_token)
            self._cached_token = fusion_jwt
            self._expires_at = expires_at
            log.info(
                "Fusion JWT refreshed, expires in %d s (at %s)",
                int(expires_at - now),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)),
            )
            return self._cached_token

    # ------------------------------------------------------------------
    # Step 1: keytab -> gssapi -> SPNEGO negotiate -> PF token
    # ------------------------------------------------------------------
    def _negotiate_pingfed_token(self) -> str:
        """SPNEGO-negotiate against the SSO host using keytab credentials.

        Uses ``httpx`` + ``httpx_gssapi.HTTPSPNEGOAuth`` (same libraries as
        ``erp_auth_helper.py``).  Falls back to ``requests`` +
        ``requests_kerberos`` if ``httpx_gssapi`` is not available.
        """
        keytab = self._cfg.keytab_path
        principal = self._cfg.principal
        url = self._cfg.negotiate_url
        timeout = self._cfg.timeout_seconds

        # --- Try httpx + httpx_gssapi first (matching erp_auth_helper.py) ---
        try:
            import httpx  # type: ignore[import-untyped]
            from httpx_gssapi import HTTPSPNEGOAuth  # type: ignore[import-untyped]

            # Set keytab so gssapi picks it up
            os.environ["KRB5_CLIENT_KTNAME"] = keytab
            auth = HTTPSPNEGOAuth()

            ssl_ctx = _get_erp_ssl_context()
            resp = httpx.get(
                url,
                auth=auth,
                timeout=timeout,
                verify=ssl_ctx,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"SPNEGO negotiate failed (HTTP {resp.status_code}): "
                    f"{str(resp.text)[:300]}"
                )

            return self._extract_pf_token(resp)

        except ImportError:
            log.debug("httpx_gssapi not available, trying requests_kerberos")

        # --- Fallback: requests + requests_kerberos ---
        try:
            import gssapi  # type: ignore[import-untyped]
            import requests  # type: ignore[import-untyped]
            from requests_kerberos import (  # type: ignore[import-untyped]
                HTTPKerberosAuth,
                DISABLED,
            )
        except ImportError as exc:
            raise ConfigError(
                "Keytab mode requires either 'httpx + httpx_gssapi' or "
                "'requests + gssapi + requests_kerberos'. Install one set "
                "or switch to mode=static."
            ) from exc

        name = gssapi.Name(principal, gssapi.NameType.user)
        creds = gssapi.Credentials.acquire(
            name=name,
            usage="initiate",
            store={"client_keytab": keytab},
        ).creds
        auth_rk = HTTPKerberosAuth(mutual_authentication=DISABLED, creds=creds)

        resp_rk = requests.get(
            url,
            auth=auth_rk,
            timeout=timeout,
            verify=False,  # noqa: S501 — matches erp_auth_helper SSL context
        )
        if resp_rk.status_code >= 400:
            raise RuntimeError(
                f"SPNEGO negotiate failed (HTTP {resp_rk.status_code}): "
                f"{str(resp_rk.text)[:300]}"
            )

        return self._extract_pf_token(resp_rk)

    @staticmethod
    def _extract_pf_token(resp: Any) -> str:
        """Pull the PF access token from a negotiate response."""
        try:
            body = resp.json()
        except (ValueError, AttributeError):
            return str(resp.text).strip()

        token = (
            body.get("access_token")
            or body.get("token")
            or body.get("id_token")
        )
        if not token:
            raise RuntimeError(
                f"SPNEGO negotiate response did not contain a token: {body}"
            )
        return str(token)

    # ------------------------------------------------------------------
    # Step 2: PF token -> ERPSecurity/CreateJWT -> Oracle Fusion JWT
    # ------------------------------------------------------------------
    def _exchange_for_oracle_jwt(self, pf_token: str) -> tuple[str, float]:
        """POST the PingFed token to ERPSecurity/CreateJWT.

        Follows the erp_auth_helper pattern::

            r = httpx.post(
                erp_auth_url,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + pftoken},
                json={},            # <-- empty body
                timeout=3000,
            )
            jwt_token = r.json()["token"]

        Returns:
            (fusion_jwt, expires_at_epoch_seconds)
        """
        erp_auth_url = self._cfg.erp_auth_url
        timeout = self._cfg.timeout_seconds

        log.debug(
            "Exchanging PF token for Oracle JWT via %s", erp_auth_url
        )

        # Use httpx if available (matching erp_auth_helper), else requests
        try:
            import httpx  # type: ignore[import-untyped]

            ssl_ctx = _get_erp_ssl_context()
            resp = httpx.post(
                erp_auth_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {pf_token}",
                },
                json={},
                timeout=timeout,
                verify=ssl_ctx,
            )
            resp.raise_for_status()
            body = resp.json()

        except ImportError:
            import requests  # type: ignore[import-untyped]

            resp_rq = requests.post(
                erp_auth_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {pf_token}",
                },
                json={},
                timeout=timeout,
                verify=False,  # noqa: S501
            )
            resp_rq.raise_for_status()
            body = resp_rq.json()

        fusion_jwt = body.get("token")
        if not fusion_jwt:
            raise RuntimeError(
                f"ERPSecurity/CreateJWT did not return a token: {body}"
            )
        assert isinstance(fusion_jwt, str)

        # Derive expiry from JWT 'exp' claim
        expires_at = _extract_jwt_expiry(fusion_jwt)
        return fusion_jwt, expires_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_jwt_expiry(jwt: str) -> float:
    """Decode a JWT and return its 'exp' claim as epoch seconds.

    Falls back to now+1hour if the token is opaque or lacks an exp claim.
    """
    try:
        parts = jwt.split(".")
        if len(parts) != 3:
            raise ValueError("not a JWS")
        # Payload is base64url with padding stripped
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp:
            return float(exp)
    except Exception:
        log.debug(
            "Could not decode JWT expiry; defaulting to +1h", exc_info=True
        )
    return time.time() + 3600


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def build_token_provider(
    fusion_auth_cfg: Optional[Dict[str, Any]],
    legacy_token_env: Optional[str] = None,
) -> Optional[Callable[[], str]]:
    """Build a token provider callable from the ``fusion_auth`` config block.

    Returns a zero-arg callable that yields a currently-valid Fusion JWT,
    or ``None`` if neither ``fusion_auth`` nor ``legacy_token_env`` resolves
    to a usable token (caller falls back to the legacy ``bearer_token_env``
    inside OracleConfig/BIPConfig).
    """
    if fusion_auth_cfg:
        cfg = TokenProviderConfig.from_dict(fusion_auth_cfg)
        provider = FusionTokenProvider(cfg)
        # Eager fetch so config errors surface early, not mid-run.
        if cfg.mode == "keytab":
            provider.get_token()
        log.info("Built Fusion token provider (mode=%s)", cfg.mode)
        return provider.get_token

    if legacy_token_env:
        token = os.environ.get(legacy_token_env, "").strip()
        if token:
            return lambda: token

    return None
