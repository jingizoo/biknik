"""Fusion JWT token provider — keytab -> PingFed -> JWT Service -> Oracle JWT.

This module replaces the static ``FUSION_JWT`` env-var pattern with a token
provider that can refresh itself.  The flow on the Citadel batch pod is::

    keytab file (mounted from vault)
          |
          v  [kinit via gssapi]
    Kerberos credentials
          |
          v  [SPNEGO auth to PingFed]
    PingFed access_token
          |
          v  [POST to internal JWT exchange service]
    Oracle Fusion JWT  (cached until ~5 min before expiry)

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

    # Keytab flow (production)
    "fusion_auth": {
        "mode": "keytab",
        "keytab_path_env": "FUSION_KEYTAB_PATH",
        "principal_env": "FUSION_PRINCIPAL",
        "pingfed_url": "https://pingfed.citadelgroup.com/.../token",
        "jwt_service_url": "https://internal-jwt-svc/exchange",
        "jwt_service_scope": "oracle-fusion",
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
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests  # type: ignore[import-untyped]

from .exceptions import ConfigError
from .proxy_config import get_proxy_config

log = logging.getLogger(__name__)


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
    pingfed_url: str = ""
    jwt_service_url: str = ""
    jwt_service_scope: str = ""

    # --- common ----------------------------------------------------------
    cache_buffer_seconds: int = 300  # refresh 5 min before expiry
    timeout_seconds: int = 30
    verify_ssl: bool = True
    require_proxy: bool = False

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
                    "fusion_auth.token or fusion_auth.token_env is required for mode=static"
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
                "fusion_auth.keytab_path or keytab_path_env is required for mode=keytab"
            )

        principal = d.get("principal", "").strip()
        if not principal:
            env_key = d.get("principal_env", "").strip()
            if env_key:
                principal = os.environ.get(env_key, "").strip()
        if not principal:
            raise ConfigError(
                "fusion_auth.principal or principal_env is required for mode=keytab"
            )

        pingfed_url = str(d.get("pingfed_url", "")).strip()
        jwt_service_url = str(d.get("jwt_service_url", "")).strip()
        if not pingfed_url or not jwt_service_url:
            raise ConfigError(
                "fusion_auth.pingfed_url and jwt_service_url are required for mode=keytab"
            )

        return TokenProviderConfig(
            mode="keytab",
            keytab_path=keytab,
            principal=principal,
            pingfed_url=pingfed_url,
            jwt_service_url=jwt_service_url,
            jwt_service_scope=str(d.get("jwt_service_scope", "")).strip(),
            cache_buffer_seconds=int(d.get("cache_buffer_seconds", 300)),
            timeout_seconds=int(d.get("timeout_seconds", 30)),
            verify_ssl=bool(d.get("verify_ssl", True)),
            require_proxy=bool(d.get("require_proxy", False)),
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------
class FusionTokenProvider:
    """Caches + refreshes an Oracle Fusion JWT via keytab -> PingFed -> JWT service.

    Thread-safe: a single refresh is serialised with a lock so concurrent
    callers can't stampede the PingFed / JWT service endpoints.
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

            log.info("Refreshing Fusion JWT via keytab -> PingFed -> JWT service")
            ping_token = self._fetch_pingfed_token()
            fusion_jwt, expires_at = self._exchange_for_fusion_jwt(ping_token)
            self._cached_token = fusion_jwt
            self._expires_at = expires_at
            log.info(
                "Fusion JWT refreshed, expires in %d s (at %s)",
                int(expires_at - now),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)),
            )
            return self._cached_token

    # ------------------------------------------------------------------
    # Step 1: keytab -> Kerberos creds -> PingFed access_token (SPNEGO)
    # ------------------------------------------------------------------
    def _fetch_pingfed_token(self) -> str:
        """Call PingFed with kerberos (SPNEGO) auth using the keytab.

        Requires ``gssapi`` and ``requests-kerberos`` libraries in the
        image (``pip install gssapi requests-kerberos``).
        """
        try:
            import gssapi  # type: ignore[import-untyped]
            from requests_kerberos import (  # type: ignore[import-untyped]
                HTTPKerberosAuth,
                DISABLED,
            )
        except ImportError as exc:
            raise ConfigError(
                "Keytab mode requires 'gssapi' and 'requests-kerberos'; "
                "install them or switch to mode=static"
            ) from exc

        # Acquire creds from keytab (no external kinit call)
        name = gssapi.Name(self._cfg.principal, gssapi.NameType.user)
        creds = gssapi.Credentials.acquire(
            name=name,
            usage="initiate",
            store={"client_keytab": self._cfg.keytab_path},
        ).creds

        auth = HTTPKerberosAuth(mutual_authentication=DISABLED, creds=creds)

        proxies = get_proxy_config(require=self._cfg.require_proxy)
        resp = requests.get(
            self._cfg.pingfed_url,
            auth=auth,
            timeout=self._cfg.timeout_seconds,
            verify=self._cfg.verify_ssl,
            proxies=proxies,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"PingFed auth failed (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        # Most PingFed token endpoints return JSON with access_token.
        # If Citadel's endpoint returns a different shape, adjust here.
        try:
            body = resp.json()
        except ValueError:
            # Some endpoints return the raw token as plain text
            return resp.text.strip()

        token = body.get("access_token") or body.get("token")
        if not token:
            raise RuntimeError(
                f"PingFed response did not contain access_token: {body}"
            )
        return str(token)

    # ------------------------------------------------------------------
    # Step 2: PingFed token -> internal JWT service -> Oracle Fusion JWT
    # ------------------------------------------------------------------
    def _exchange_for_fusion_jwt(self, ping_token: str) -> tuple[str, float]:
        """POST the Ping token to the internal JWT service, return Fusion JWT.

        Returns:
            (fusion_jwt, expires_at_epoch_seconds)
        """
        proxies = get_proxy_config(require=self._cfg.require_proxy)

        payload: Dict[str, Any] = {"token": ping_token}
        if self._cfg.jwt_service_scope:
            payload["scope"] = self._cfg.jwt_service_scope

        resp = requests.post(
            self._cfg.jwt_service_url,
            json=payload,
            headers={"Authorization": f"Bearer {ping_token}"},
            timeout=self._cfg.timeout_seconds,
            verify=self._cfg.verify_ssl,
            proxies=proxies,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"JWT service exchange failed (HTTP {resp.status_code}): "
                f"{resp.text[:300]}"
            )

        body = resp.json()
        fusion_jwt = (
            body.get("access_token")
            or body.get("jwt")
            or body.get("token")
        )
        if not fusion_jwt:
            raise RuntimeError(
                f"JWT service response did not contain a token: {body}"
            )

        # Try to derive expiry — prefer server-provided, else decode JWT 'exp'.
        expires_in = body.get("expires_in")
        if expires_in:
            expires_at = time.time() + int(expires_in)
        else:
            expires_at = _extract_jwt_expiry(str(fusion_jwt))

        return str(fusion_jwt), expires_at


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
        log.debug("Could not decode JWT expiry; defaulting to +1h", exc_info=True)
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
