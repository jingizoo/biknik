"""Notification delivery queue + mock worker (#58).

When a feed notification is emitted it is also fanned out to one
:class:`NotificationDelivery` row per channel (email, push). A worker later
drains the queue through a *mock* sender — there is no real SMTP / APNs / FCM
in this slice. Failed sends are retried until an attempt budget is exhausted.

Everything here is pure and deterministic: the clock is injected and the
sender is pluggable, so success / failure / retry are all unit-testable.
"""

import threading

from ..domain import (
    DeliveryStatus,
    NotificationAudience,
    NotificationChannel,
    NotificationDelivery,
)
from ..domain.errors import ValidationError
from .email_transport import DryRunEmailTransport
from .push_transport import DryRunPushTransport

# Channels every notification fans out to in this slice.
DEFAULT_CHANNELS = (NotificationChannel.EMAIL, NotificationChannel.PUSH)

# How many times a single delivery is attempted before it is left failed.
MAX_ATTEMPTS = 3

# Worker-loop defaults (#79). Disabled by default so tests and the demo never
# spin a background thread unless one is explicitly asked for.
DEFAULT_WORKER_INTERVAL = 30
DEFAULT_WORKER_BATCH = 50


def recipient_ref(notification) -> str:
    """Who a notification's deliveries target, derived from its audience (#59).

    Officials and coaches resolve to the specific official / team the
    notification was addressed to; scheduler and public map to shared
    operator / broadcast targets. This is the routing key a real transport
    would later look up a mailbox or device token by.

    Targeted audiences fail closed (#60): an OFFICIAL / COACH notification with
    no ``audience_ref`` — or an unknown audience — raises rather than silently
    broadening to ``public``, which can now point at a stored contact.
    """
    aud = notification.audience
    ref = notification.audience_ref
    if aud == NotificationAudience.OFFICIAL:
        if not ref:
            raise ValidationError("An official notification needs an audience_ref.")
        return "official:" + ref
    if aud == NotificationAudience.COACH:
        if not ref:
            raise ValidationError("A coach notification needs an audience_ref.")
        return "team:" + ref
    if aud == NotificationAudience.PLAYER:
        if not ref:
            raise ValidationError("A player notification needs an audience_ref.")
        return "player:" + ref
    if aud == NotificationAudience.SCHEDULER:
        return "scheduler"
    if aud == NotificationAudience.PUBLIC:
        return "public"
    raise ValidationError(f"Unsupported notification audience '{aud}'.")


def destination_for(recipient: str, channel) -> str:
    """A placeholder per-channel address for a recipient (#59).

    No real mailbox or device token exists in this slice, so we synthesize an
    obviously-fictional address. The ``.invalid`` TLD is reserved and never
    routes, which keeps the mock worker from ever contacting anything real.
    """
    slug = recipient.replace(":", "-")
    if channel == NotificationChannel.EMAIL:
        return slug + "@notify.invalid"
    return "push-token:" + slug


def resolve_destination(store, recipient: str, channel) -> str:
    """The real stored destination for (recipient, channel), else a placeholder.

    Resolution order:
      1. push only — the recipient's first *active* device token (#65);
      2. an *active* registered contact destination (#60);
      3. the synthesized ``.invalid`` / ``push-token:`` placeholder (#59),
         so a delivery always has somewhere (fictional) to go.

    A retired contact (``active=False``, #232 review 6) is never resolved
    to — retiring one to clear a Player/Official delete's dependency must
    actually stop it from being live, not just stop it from blocking the
    delete. Every retry/re-resolution (below) re-checks this, so a delivery
    enqueued before retirement falls back to the placeholder on its very
    next attempt rather than continuing to reach the retired address.
    """
    if channel == NotificationChannel.PUSH:
        token = store.active_device_token_for(recipient)
        if token is not None and token.token:
            return token.token
    stored = store.get_contact_destination(recipient, channel)
    if stored is not None and stored.destination and stored.active:
        return stored.destination
    return destination_for(recipient, channel)


def channel_enabled(store, recipient: str, channel) -> bool:
    """Whether ``recipient`` accepts deliveries on ``channel`` (#81).

    A stored preference with ``enabled=False`` opts the channel out; absent
    preferences mean the channel is on (existing default behavior). The in-app
    feed is always delivered and is not gated here.
    """
    pref = store.get_notification_preference(recipient, channel)
    return True if pref is None else pref.enabled


def _guardian_recipient_refs(store, player_id) -> list:
    """Verified guardians linked to ``player_id``, as delivery recipient refs
    (#32). An unverified link carries no authority — same gate the guardian
    action routes use (#26) — so it gets no delivery either."""
    return ["guardian:" + g.guardian_user_id
            for g in store.guardian_links_for_player(player_id) if g.verified]


def enqueue(store, notification, channels=DEFAULT_CHANNELS):
    """Create the pending delivery rows for a freshly emitted notification.

    A channel the recipient has disabled in their preferences (#81) is skipped
    — no delivery row is created for it — so the resolver honors opt-outs.

    A PLAYER-addressed notification also reaches any verified guardian linked
    to that junior (#32) — a junior may not check their own device, so their
    guardian needs the same push/email delivery. This fans out here, in the
    delivery layer itself, so every existing and future PLAYER-audience
    emission site (today: substitute offers) gets it automatically without
    each call site needing to know guardians exist.
    """
    recipients = [recipient_ref(notification)]
    if notification.audience == NotificationAudience.PLAYER:
        recipients.extend(
            _guardian_recipient_refs(store, notification.audience_ref))
    created = []
    for recipient in recipients:
        for channel in channels:
            if not channel_enabled(store, recipient, channel):
                continue
            d = NotificationDelivery(
                id=store.next_id("notif_delivery"),
                notification_id=notification.id,
                channel=channel,
                recipient_ref=recipient,
                destination=resolve_destination(store, recipient, channel),
            )
            created.append(store.add_notification_delivery(d))
    return created


def mock_sender(delivery, notification) -> None:
    """A no-op sender for non-email channels (push is still mocked, #62).

    Raising an exception signals a failed send, which the worker records and
    retries.
    """
    return None


def make_delivery_sender(email_transport, push_transport=None):
    """A sender that routes each delivery to its channel transport (#62/#64).

    Email deliveries go through ``email_transport`` and push deliveries through
    ``push_transport`` (a safe dry-run by default). Any exception from either
    path signals a failed send.
    """
    push_transport = push_transport or DryRunPushTransport()

    def sender(delivery, notification) -> None:
        if delivery.channel == NotificationChannel.EMAIL:
            email_transport.send(delivery, notification)
        else:
            push_transport.send(delivery, notification)
    return sender


class DeliveryWorker:
    """Drains pending notification deliveries through the configured transports.

    Email and push deliveries go through their transports (both safe dry-runs
    by default, so nothing is sent for real unless a real transport is
    configured). A fully custom ``sender`` can still be injected for tests.
    """

    def __init__(self, store, clock, sender=None,
                 max_attempts: int = MAX_ATTEMPTS, email_transport=None,
                 push_transport=None):
        self.store = store
        self.clock = clock
        self.email_transport = email_transport or DryRunEmailTransport()
        self.push_transport = push_transport or DryRunPushTransport()
        self.sender = sender or make_delivery_sender(
            self.email_transport, self.push_transport)
        self.max_attempts = max_attempts
        # One process-wide lock guards the read-send-save critical section so
        # the manual drain endpoint and the background worker loop (#79) cannot
        # both claim the same pending rows and send them twice. Both paths call
        # process_pending on the SAME worker instance, so the lock must live on
        # the worker, not on the loop.
        self._process_lock = threading.Lock()

    def process_pending(self, limit=None) -> dict:
        """Attempt every deliverable row once; return a run summary.

        Deliverable = still ``pending``, or ``failed`` with attempts left.
        A row that fails its final attempt is moved to ``dead_lettered`` and is
        not picked up again until an operator retries it (#80). ``limit`` caps
        how many rows are processed in one call (the worker loop's batch size,
        #79); ``None`` processes all of them.

        Serialized by ``self._process_lock`` so concurrent callers (manual
        drain + background loop) drain the queue one batch at a time rather
        than double-sending overlapping rows.
        """
        with self._process_lock:
            return self._process_pending_locked(limit)

    def _process_pending_locked(self, limit=None) -> dict:
        processed = sent = failed = dead_lettered = 0
        rows = self.store.pending_deliveries(self.max_attempts)
        if limit is not None:
            rows = rows[:limit]
        for d in rows:
            notification = self.store.get_notification_feed(d.notification_id)
            # Re-resolve the destination on every attempt so a contact or
            # device token registered AFTER emission applies to queued and
            # retrying deliveries (they would otherwise fail forever on the
            # placeholder stamped at enqueue time).
            if d.recipient_ref:
                d.destination = resolve_destination(
                    self.store, d.recipient_ref, d.channel)
            now = self.clock()
            d.attempts += 1
            d.last_attempt_at = now
            try:
                self.sender(d, notification)
            except Exception as exc:  # a mock/real transport failure
                d.last_error = str(exc) or exc.__class__.__name__
                if d.attempts >= self.max_attempts:
                    # Attempt budget exhausted: park it for the operator (#80).
                    d.status = DeliveryStatus.DEAD_LETTERED
                    d.dead_lettered_at = now
                    d.next_attempt_at = None
                    dead_lettered += 1
                else:
                    d.status = DeliveryStatus.FAILED
                    d.next_attempt_at = now  # eligible immediately in this slice
                failed += 1
            else:
                d.status = DeliveryStatus.SENT
                d.sent_at = now
                d.last_error = None
                d.next_attempt_at = None
                sent += 1
            self.store.save_notification_delivery(d)
            processed += 1
        return {"processed": processed, "sent": sent, "failed": failed,
                "dead_lettered": dead_lettered}


class DeliveryLoop:
    """A controllable loop around :class:`DeliveryWorker` (#79).

    Turns manual drains into an opt-in background worker: ``run_once`` for a
    single bounded batch, ``start``/``stop`` for a daemon loop on a fixed
    interval. Disabled by default so nothing runs unless explicitly enabled —
    tests and the demo never spin a thread implicitly. The underlying worker
    (and its dry-run/live transports) is unchanged; the loop only decides
    *when* and *how many* rows to process.
    """

    def __init__(self, worker: DeliveryWorker, enabled: bool = False,
                 interval_seconds: int = DEFAULT_WORKER_INTERVAL,
                 batch_size: int = DEFAULT_WORKER_BATCH):
        self.worker = worker
        self.enabled = bool(enabled)
        # Preserve a fractional interval (tests drive sub-second loops); only
        # clamp non-positive values up to a small positive floor so a bad
        # config can never spin a hot loop.
        self.interval_seconds = max(float(interval_seconds), 0.001)
        self.batch_size = int(batch_size)
        self._thread = None
        self._stop = threading.Event()

    def run_once(self) -> dict:
        """Process a single batch (up to ``batch_size`` rows) and return the
        run summary. Works regardless of ``enabled`` — it is the manual drain."""
        return self.worker.process_pending(limit=self.batch_size)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the background loop. No-op (returns False) when disabled or
        already running; returns True when a thread was started."""
        if not self.enabled or self.is_running():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="delivery-worker")
        self._thread.start()
        return True

    def _loop(self) -> None:
        # Wait first so start() returns promptly; keep looping until stopped.
        # A transport error in one batch must not kill the loop.
        while not self._stop.wait(self.interval_seconds):
            try:
                self.run_once()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        """Serializable worker status for the delivery overview (#79)."""
        return {"enabled": self.enabled, "running": self.is_running(),
                "interval_seconds": self.interval_seconds,
                "batch_size": self.batch_size}


def delivery_loop_from_env(worker: DeliveryWorker, env) -> DeliveryLoop:
    """Build a DeliveryLoop from environment config (#79).

    DELIVERY_WORKER_ENABLED (truthy: 1/true/yes/on) enables the loop;
    DELIVERY_WORKER_INTERVAL and DELIVERY_WORKER_BATCH override the interval
    (seconds) and batch size. Absent/blank → safe disabled defaults.
    """
    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "on")

    def _int(v, default):
        try:
            n = int(str(v).strip())
            return n if n > 0 else default
        except (TypeError, ValueError):
            return default

    return DeliveryLoop(
        worker,
        enabled=_truthy(env.get("DELIVERY_WORKER_ENABLED")),
        interval_seconds=_int(env.get("DELIVERY_WORKER_INTERVAL"),
                              DEFAULT_WORKER_INTERVAL),
        batch_size=_int(env.get("DELIVERY_WORKER_BATCH"), DEFAULT_WORKER_BATCH))
