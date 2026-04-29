"""Proxy configuration helper for ofam_mass_additions.

Self-contained copy of the env-var-driven proxy discovery used by the
asset-xfer pipeline.  Kept inside this package so the runner has zero
import-time dependency on ofam_asset_xfer.

Reads ``HTTP_APP_PROXY``, ``HTTPS_APP_PROXY``, ``INETPROXY_USER``, and
``INETPROXY_PASSWD`` from the environment and returns a dict suitable
for ``requests.Session.proxies``.
"""

from __future__ import annotations

import logging
import os
from typing import Dict
from urllib.parse import quote_plus

log = logging.getLogger(__name__)


def get_proxy_config(require: bool = False) -> Dict[str, str]:
    """Build a ``requests``-compatible proxies dict from environment variables.

    Args:
        require: If True, raise ``ValueError`` when proxy env vars are
            missing instead of silently returning an empty dict.  Use
            this in pod / batch environments where direct routes don't
            exist.

    Returns:
        Dict suitable for ``requests.Session.proxies`` or
        ``requests.get(proxies=...)``.  Empty dict when proxy env vars
        are absent and ``require=False``.
    """
    http_proxy_url = os.environ.get("HTTP_APP_PROXY")
    https_proxy_url = os.environ.get("HTTPS_APP_PROXY")

    if not http_proxy_url and not https_proxy_url:
        if require:
            raise ValueError(
                "Proxy is required but HTTP_APP_PROXY / HTTPS_APP_PROXY env "
                "vars are not set. Set them or pass require=False for local dev."
            )
        log.debug(
            "No proxy env vars found (HTTP_APP_PROXY / HTTPS_APP_PROXY); skipping proxy."
        )
        return {}

    user = os.environ.get("INETPROXY_USER", "")
    passwd = os.environ.get("INETPROXY_PASSWD", "")

    if not user or not passwd:
        if require:
            raise ValueError(
                "Proxy env vars set but INETPROXY_USER / INETPROXY_PASSWD are "
                "missing. Check the K8s secret (Holocron Vault)."
            )
        log.warning(
            "Proxy URLs set but INETPROXY_USER/PASSWD missing — proxy will be unauthenticated."
        )

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
    """Insert ``user:password@`` into *raw_url* when credentials are provided."""
    if not user:
        return raw_url
    host_part = raw_url.replace("http://", "").replace("https://", "")
    return f"http://{quote_plus(user)}:{quote_plus(passwd)}@{host_part}"
