"""Proxy configuration for Oracle URL access.

Reads INETPROXY_USER, INETPROXY_PASSWD, HTTP_APP_PROXY, and HTTPS_APP_PROXY
from environment variables and builds authenticated proxy URLs for use with
the ``requests`` library.

When the environment variables are not set, returns an empty dict (no proxy).
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional
from urllib.parse import quote_plus

log = logging.getLogger(__name__)


def get_proxy_config() -> Dict[str, str]:
    """Build a ``requests``-compatible proxies dict from environment variables.

    Expected env vars:
        INETPROXY_USER     – proxy username
        INETPROXY_PASSWD   – proxy password
        HTTP_APP_PROXY     – HTTP proxy URL  (e.g. http://proxy.example.com:80)
        HTTPS_APP_PROXY    – HTTPS proxy URL (e.g. http://proxy.example.com:443)

    Returns:
        Dict suitable for ``requests.Session.proxies`` or ``requests.get(proxies=...)``.
        Empty dict when proxy env vars are absent.
    """
    http_proxy_url = os.environ.get("HTTP_APP_PROXY")
    https_proxy_url = os.environ.get("HTTPS_APP_PROXY")

    if not http_proxy_url and not https_proxy_url:
        log.debug("No proxy env vars found (HTTP_APP_PROXY / HTTPS_APP_PROXY); skipping proxy.")
        return {}

    user = os.environ.get("INETPROXY_USER", "")
    passwd = os.environ.get("INETPROXY_PASSWD", "")

    proxies: Dict[str, str] = {}

    if http_proxy_url:
        proxies["http"] = _build_proxy_url(user, passwd, http_proxy_url)

    if https_proxy_url:
        proxies["https"] = _build_proxy_url(user, passwd, https_proxy_url)

    log.info("Proxy configured for protocols: %s", list(proxies.keys()))
    return proxies


def _build_proxy_url(user: str, passwd: str, raw_url: str) -> str:
    """Insert ``user:password@`` into *raw_url* when credentials are provided.

    Example:
        _build_proxy_url("alice", "p@ss", "http://proxy.corp:8080")
        → "http://alice:p%40ss@proxy.corp:8080"
    """
    if not user:
        return raw_url

    # Strip the scheme so we can re-prefix with credentials.
    host_part = raw_url.replace("http://", "").replace("https://", "")
    return f"http://{quote_plus(user)}:{quote_plus(passwd)}@{host_part}"
