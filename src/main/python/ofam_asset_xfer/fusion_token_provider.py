"""Fusion JWT token provider — keytab -> SPNEGO -> ERPSecurity/CreateJWT.

Follows the Citadel ``erp_auth_helper.py`` pattern:

    ERP_KEYTAB (base64 env var)
          |
          v  [decode keytab to temp file]
    Kerberos credentials
          |
          v  [SPNEGO negotiate via httpx + httpx_gssapi]
    PingFed access_token  (from json()["access_token"])
          |
          v  [POST to acctgateway ERPSecurity/CreateJWT, json={}, Bearer header]
    Oracle Fusion JWT  (from json()["token"])
          |
          v  [cached until ~5 min before 'exp' claim]

Environment is resolved from ``CITADEL_ENV`` env var and drives which
SSO host and ERPSecurity endpoint is used.

Configuration
-------------
Two modes::

    # Static JWT (dev / workstation)
    "fusion_auth": { "mode": "static", "token_env": "FUSION_JWT" }

    # Keytab flow (batch pod — keytab is base64 in ERP_KEYTAB env var)
    "fusion_auth": { "mode": "keytab" }

Environment variables (keytab mode):
    ERP_KEYTAB          Base64-encoded keytab binary (from Holocron Vault)
    ERP_KEYTAB_PATH     Optional path to write decoded keytab (else tempfile)
    CITADEL_ENV         Environment name (stabledev, dev1-4, test, prod)

If ``fusion_auth`` is absent the entrypoint falls back to the legacy
``oracle.bearer_token_env`` / ``bip.bearer_token_env`` fields.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import ssl
import tempfile
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
    if env == "dev":
        env = "stabledev"
    return env.upper()


def _get_erp_ssl_context() -> ssl.SSLContext:
    """Build a custom SSL context matching erp_auth_helper.py for REALM3."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers(
        "AES256-SHA:DHE-RSA-AES256-SHA:AES128-SHA:DHE-RSA-AES128-SHA"
    )
    return ctx


def _safe_remove_file(filepath: Optional[str]) -> None:
    """Safely remove a temp file (matches erp_auth_helper.safe_remove_file)."""
    if filepath:
        try:
            if os.path.exists(filepath):
                os.unlink(filepath)
        except Exception:
            log.debug("Could not remove temp keytab %s", filepath)


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
        if not os.environ.get("ERP_KEYTAB"):
            raise ConfigError(
                "fusion_auth.mode=keytab requires ERP_KEYTAB env var "
                "(base64-encoded keytab from Holocron Vault)"
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
                    f"Unknown ora_env={ora_env!r}; set negotiate_url "
                    f"explicitly or use one of: {list(SSO_HOST_MAP.keys())}"
                )
            negotiate_url = f"https://{sso_host}/negotiate"

        if not erp_auth_url:
            erp_auth_url_resolved = ERP_AUTH_URL_MAP.get(ora_env)
            if not erp_auth_url_resolved:
                raise ConfigError(
                    f"Unknown ora_env={ora_env!r}; set erp_auth_url "
                    f"explicitly or use one of: {list(ERP_AUTH_URL_MAP.keys())}"
                )
            erp_auth_url = erp_auth_url_resolved

        return TokenProviderConfig(
            mode=mode,
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
    """Caches + refreshes Oracle Fusion JWT.

    Follows ``erp_auth_helper.py``:

    1. Acquire Kerberos creds from keytab (``ERP_KEYTAB`` base64 env var
       → decode → temp file → ``KRB5_CLIENT_KTNAME``).
    2. SPNEGO-negotiate against ``sso[-dev].citadelgroup.com/negotiate``
       → PingFed ``access_token``.
    3. POST PF token to ``acctgateway-erpsecurity-*.citadelgroup.com
       /ERPSecurity/CreateJWT`` with empty JSON body → Oracle JWT
       (response field ``"token"``).
    4. Cache the JWT and refresh ~5 min before its ``exp`` claim.
    """

    def __init__(self, cfg: TokenProviderConfig):
        """Initialise the provider with the given config."""
        self._cfg = cfg
        self._lock = threading.Lock()
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """Return a currently-valid Fusion JWT, refreshing if near expiry."""
        if self._cfg.mode == "static":
            return self._cfg.static_token

        now = time.time()
        if (
            self._cached_token
            and now < (self._expires_at - self._cfg.cache_buffer_seconds)
        ):
            return self._cached_token

        with self._lock:
            now = time.time()
            if (
                self._cached_token
                and now < (self._expires_at - self._cfg.cache_buffer_seconds)
            ):
                return self._cached_token

            log.info(
                "Refreshing Fusion JWT (env=%s, mode=%s)",
                self._cfg.ora_env,
                self._cfg.mode,
            )
            pf_token = self._get_current_user_token()
            fusion_jwt, expires_at = self._get_ora_token(pf_token)
            self._cached_token = fusion_jwt
            self._expires_at = expires_at
            log.info(
                "Fusion JWT refreshed, expires in %d s (at %s)",
                int(expires_at - now),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)),
            )
            return self._cached_token

    # ------------------------------------------------------------------
    # Step 1: get_current_user_token (matches erp_auth_helper exactly)
    # ------------------------------------------------------------------
    def _get_current_user_token(self) -> str:
        """SPNEGO-negotiate against SSO to obtain a PingFed access_token.

        Keytab credentials only (``ERP_KEYTAB`` base64):
          - Decode base64 → write to ``ERP_KEYTAB_PATH`` or tempfile
          - Set ``KRB5_CLIENT_KTNAME``
          - ``HTTPSPNEGOAuth`` with SPNEGO mech (picks up keytab via env)
          - Clean up temp file after
        """
        try:
            import gssapi  # type: ignore[import-untyped]
            import httpx  # type: ignore[import-not-found]
            from httpx_gssapi import HTTPSPNEGOAuth  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ConfigError(
                "Keytab mode requires 'gssapi', 'httpx', and 'httpx_gssapi'. "
                "Install them or switch to mode=static."
            ) from exc

        negotiate_url = self._cfg.negotiate_url
        spnego = gssapi.mechs.Mechanism.from_sasl_name("SPNEGO")
        keytab_temp_path: Optional[str] = None

        try:
            erp_keytab_b64 = os.environ["ERP_KEYTAB"]

            erp_keytab_path = os.environ.get("ERP_KEYTAB_PATH", "")
            if erp_keytab_path:
                with open(erp_keytab_path, "wb") as kt_out:
                    kt_out.write(base64.b64decode(erp_keytab_b64))
                keytab_out_location = erp_keytab_path
            else:
                keytab_fd, keytab_temp_name = tempfile.mkstemp()
                keytab_fp = os.fdopen(keytab_fd, "wb")
                keytab_fp.write(base64.b64decode(erp_keytab_b64))
                keytab_fp.close()
                keytab_out_location = keytab_temp_name
                keytab_temp_path = keytab_temp_name

            os.environ["KRB5_CLIENT_KTNAME"] = keytab_out_location
            log.debug("Using spnego credentials (keytab at %s)", keytab_out_location)

            auth = HTTPSPNEGOAuth(mech=spnego)

            # --- SPNEGO negotiate ---
            ssl_ctx = _get_erp_ssl_context()
            token_data = httpx.get(
                negotiate_url,
                auth=auth,  # type: ignore[arg-type]
                timeout=self._cfg.timeout_seconds,
                verify=ssl_ctx,
            )
            token_data.raise_for_status()
            user_token: str = token_data.json()["access_token"]
            return user_token

        except Exception as exc:
            raise RuntimeError(
                f"Error during SPNEGO negotiate for PingFed token: {exc}"
            ) from exc
        finally:
            # Clean up temp keytab (matches erp_auth_helper.safe_remove_file)
            _safe_remove_file(keytab_temp_path)

    # ------------------------------------------------------------------
    # Step 2: get_ora_token (matches erp_auth_helper exactly)
    # ------------------------------------------------------------------
    def _get_ora_token(self, pf_token: str) -> tuple[str, float]:
        """POST the PingFed token to ERPSecurity/CreateJWT.

        Matches ``erp_auth_helper.get_ora_token``::

            r = httpx.post(
                erp_auth_url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + pftoken,
                },
                json={},
                timeout=3000,
            )
            jwt_token = r.json()["token"]

        Returns:
            (fusion_jwt, expires_at_epoch_seconds)
        """
        erp_auth_url = self._cfg.erp_auth_url

        log.debug("Exchanging PF token for Oracle JWT via %s", erp_auth_url)

        # httpx was already imported (and required) by _get_current_user_token
        # before we ever get here, so the previous ImportError fallback to
        # requests was unreachable.  Use httpx directly.
        import httpx  # type: ignore[import-not-found]

        ssl_ctx = _get_erp_ssl_context()
        resp = httpx.post(
            erp_auth_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + pf_token,
            },
            json={},
            timeout=3000,
            verify=ssl_ctx,
        )
        resp.raise_for_status()
        jwt_token: str = resp.json()["token"]
        assert isinstance(jwt_token, str)

        expires_at = _extract_jwt_expiry(jwt_token)
        return jwt_token, expires_at


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
        if cfg.mode == "keytab":
            provider.get_token()  # eager fetch → surface errors early
        log.info("Built Fusion token provider (mode=%s)", cfg.mode)
        return provider.get_token

    if legacy_token_env:
        token = os.environ.get(legacy_token_env, "").strip()
        if token:
            return lambda: token

    return None
