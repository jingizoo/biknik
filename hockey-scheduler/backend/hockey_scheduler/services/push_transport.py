"""Push transport adapters for the delivery worker (#64).

Mirrors the email transport (#62/#63): the worker sends *push* deliveries
through a :class:`PushTransport`. The default is a safe **dry-run** that
records what would be sent without any network access. A provider adapter
also exists, but nothing is sent for real unless it is explicitly configured
— an operator must set ``PUSH_MODE=live`` with an endpoint (or wire a
:class:`ProviderPushTransport` directly).

Only the Python standard library is used (``urllib``/``json``), so the suite
runs anywhere with no third-party dependency.
"""

# Device tokens synthesized as placeholders in #59 carry this prefix; a real
# transport must never try to deliver to one.
_PLACEHOLDER_PREFIX = "push-token:"


def _clean(value):
    value = (value or "").strip()
    return value or None


class PushTransport:
    """Interface: deliver one push. Raise on failure so the worker retries."""

    mode = "none"
    provider = None  # provider label, when the transport has one (live)

    def send(self, delivery, notification) -> None:
        raise NotImplementedError


class DryRunPushTransport(PushTransport):
    """Records outgoing push instead of sending it — the safe default."""

    mode = "dry_run"

    def __init__(self):
        self.outbox = []

    def send(self, delivery, notification) -> None:
        self.outbox.append({
            "token": delivery.destination,
            "title": notification.title if notification else "",
            "body": notification.message if notification else "",
        })


class ProviderPushTransport(PushTransport):
    """Posts to a configured push provider endpoint. Used only when configured.

    ``client`` is injectable so the network call can be faked in tests; in
    production it defaults to a real HTTP POST via :mod:`urllib`.
    """

    mode = "live"

    def __init__(self, endpoint, key=None, provider: str = "push",
                 client=None, timeout: int = 10):
        if not endpoint:
            raise ValueError("ProviderPushTransport requires an endpoint.")
        self.endpoint = endpoint
        self.key = key
        self.provider = provider
        self.timeout = timeout
        self._client = client  # injectable for tests

    def _post(self, payload) -> None:
        if self._client is not None:
            return self._client(self.endpoint, self.key, payload)
        import json
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()

    def send(self, delivery, notification) -> None:
        # Fail closed BEFORE any network call: never push to a blank or
        # placeholder token — the worker records it failed and retries (#64).
        token = (delivery.destination or "").strip()
        if not token:
            raise ValueError("Refusing to push: no device token.")
        if token.startswith(_PLACEHOLDER_PREFIX):
            raise ValueError(
                f"Refusing to push to placeholder token '{token}' — register a "
                f"real device token for this recipient first.")
        self._post({
            "token": token,
            "title": notification.title if notification else "",
            "body": notification.message if notification else "",
        })


def push_transport_from_config(config=None) -> PushTransport:
    """Build a transport from config — dry-run unless ``mode == 'live'``.

    Fail-safe by default: without an explicit ``live`` mode *and* an endpoint,
    this returns the dry-run transport so nothing is ever sent for real by
    accident.
    """
    config = config or {}
    if config.get("mode") == "live" and config.get("endpoint"):
        return ProviderPushTransport(
            endpoint=config["endpoint"], key=config.get("key"),
            provider=config.get("provider") or "push")
    return DryRunPushTransport()


def push_config_from_env(env) -> dict:
    """Map the PUSH_MODE / PUSH_* environment variables to a config dict (#64).

    Parsing is fail-safe: a whitespace-only ``PUSH_ENDPOINT`` never enables a
    live transport, so the app stays in its safe default.
    """
    return {
        "mode": (env.get("PUSH_MODE") or "dry_run").strip().lower(),
        "endpoint": _clean(env.get("PUSH_ENDPOINT")),
        "key": env.get("PUSH_KEY") or None,
        "provider": _clean(env.get("PUSH_PROVIDER")) or "push",
    }


def push_transport_from_env(env) -> PushTransport:
    """Build the configured push transport from environment variables (#64)."""
    return push_transport_from_config(push_config_from_env(env))
