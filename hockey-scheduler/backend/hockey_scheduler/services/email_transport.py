"""Email transport adapters for the delivery worker (#62).

The worker sends *email* deliveries through an :class:`EmailTransport`. The
default is a safe **dry-run** that records what would be sent without touching
the network. A real SMTP adapter also exists, but nothing is sent for real
unless it is explicitly configured — an operator must wire an
:class:`SmtpEmailTransport` (or pass ``mode="smtp"`` with a host to
:func:`email_transport_from_config`). Push and other channels are unaffected.

Only the Python standard library is used (``email``/``smtplib``), so the suite
still runs anywhere with no third-party dependency.
"""

from email.message import EmailMessage


class EmailTransport:
    """Interface: deliver one email. Raise on failure so the worker retries."""

    mode = "none"
    sender = None  # the From address, when the transport has one (SMTP)

    def send(self, delivery, notification) -> None:
        raise NotImplementedError


class DryRunEmailTransport(EmailTransport):
    """Records outgoing email instead of sending it — the safe default.

    The recorded ``outbox`` lets tests and operators see exactly what *would*
    have been sent without any network access.
    """

    mode = "dry_run"

    def __init__(self):
        self.outbox = []

    def send(self, delivery, notification) -> None:
        self.outbox.append({
            "to": delivery.destination,
            "subject": notification.title if notification else "",
            "body": notification.message if notification else "",
        })


class SmtpEmailTransport(EmailTransport):
    """Sends email via :mod:`smtplib`. Used only when explicitly configured.

    ``smtp_factory`` is injectable so the connection can be faked in tests;
    in production it defaults to a real ``smtplib.SMTP`` connection.
    """

    mode = "smtp"

    def __init__(self, host, port: int = 587, username=None, password=None,
                 sender: str = "no-reply@hockey.invalid", use_tls: bool = True,
                 smtp_factory=None, timeout: int = 10):
        if not host:
            raise ValueError("SmtpEmailTransport requires a host.")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_tls = use_tls
        self.timeout = timeout
        self._smtp_factory = smtp_factory  # injectable for tests

    def _connect(self):
        if self._smtp_factory is not None:
            return self._smtp_factory()
        import smtplib
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def _build_message(self, delivery, notification) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = delivery.destination
        msg["Subject"] = notification.title if notification else ""
        msg.set_content(notification.message if notification else "")
        return msg

    def send(self, delivery, notification) -> None:
        # Fail closed BEFORE opening any connection: never attempt a live send
        # to a blank or placeholder (.invalid) fallback address (#62). The
        # worker records this as a failed delivery and retries per its policy.
        to = (delivery.destination or "").strip()
        if not to:
            raise ValueError("Refusing to send: no destination address.")
        if to.lower().endswith(".invalid"):
            raise ValueError(
                f"Refusing to send to placeholder address '{to}' — register a "
                f"real contact for this recipient first.")
        msg = self._build_message(delivery, notification)
        smtp = self._connect()
        try:
            if self.use_tls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass


def email_transport_from_config(config=None) -> EmailTransport:
    """Build a transport from config — dry-run unless ``mode == 'smtp'``.

    Fail-safe by default: without an explicit ``smtp`` mode *and* a host, this
    returns the dry-run transport so nothing is ever sent for real by accident.
    """
    config = config or {}
    if config.get("mode") == "smtp" and config.get("host"):
        return SmtpEmailTransport(
            host=config["host"], port=config.get("port", 587),
            username=config.get("username"), password=config.get("password"),
            sender=config.get("sender", "no-reply@hockey.invalid"),
            use_tls=config.get("use_tls", True))
    return DryRunEmailTransport()


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _clean(value):
    value = (value or "").strip()
    return value or None


def email_config_from_env(env) -> dict:
    """Map the EMAIL_MODE / SMTP_* environment variables to a config dict (#63).

    Returns a plain dict so it is easy to inspect and test; passwords are read
    but never surfaced back to clients. Parsing is fail-safe: a malformed
    optional value (e.g. a non-numeric ``SMTP_PORT`` or a whitespace-only
    ``SMTP_HOST``) never crashes config loading — it falls back so the app
    stays in its safe default.
    """
    return {
        "mode": (env.get("EMAIL_MODE") or "dry_run").strip().lower(),
        "host": _clean(env.get("SMTP_HOST")),
        "port": _as_int(env.get("SMTP_PORT"), 587),
        "username": _clean(env.get("SMTP_USER")),
        "password": env.get("SMTP_PASSWORD") or None,
        "sender": _clean(env.get("SMTP_SENDER")) or "no-reply@hockey.invalid",
        "use_tls": _as_bool(env.get("SMTP_USE_TLS"), True),
    }


def email_transport_from_env(env) -> EmailTransport:
    """Build the configured transport from environment variables (#63)."""
    return email_transport_from_config(email_config_from_env(env))
