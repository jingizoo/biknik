"""Notification delivery queue + mock worker (#58).

When a feed notification is emitted it is also fanned out to one
:class:`NotificationDelivery` row per channel (email, push). A worker later
drains the queue through a *mock* sender — there is no real SMTP / APNs / FCM
in this slice. Failed sends are retried until an attempt budget is exhausted.

Everything here is pure and deterministic: the clock is injected and the
sender is pluggable, so success / failure / retry are all unit-testable.
"""

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
      2. a registered contact destination (#60);
      3. the synthesized ``.invalid`` / ``push-token:`` placeholder (#59),
         so a delivery always has somewhere (fictional) to go.
    """
    if channel == NotificationChannel.PUSH:
        token = store.active_device_token_for(recipient)
        if token is not None and token.token:
            return token.token
    stored = store.get_contact_destination(recipient, channel)
    if stored is not None and stored.destination:
        return stored.destination
    return destination_for(recipient, channel)


def enqueue(store, notification, channels=DEFAULT_CHANNELS):
    """Create the pending delivery rows for a freshly emitted notification."""
    recipient = recipient_ref(notification)
    created = []
    for channel in channels:
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

    def process_pending(self) -> dict:
        """Attempt every deliverable row once; return a run summary.

        Deliverable = still ``pending``, or ``failed`` with attempts left.
        A row that fails its final attempt stays ``failed`` and is not picked
        up again.
        """
        processed = sent = failed = 0
        for d in self.store.pending_deliveries(self.max_attempts):
            notification = self.store.get_notification_feed(d.notification_id)
            # Re-resolve the destination on every attempt so a contact or
            # device token registered AFTER emission applies to queued and
            # retrying deliveries (they would otherwise fail forever on the
            # placeholder stamped at enqueue time).
            if d.recipient_ref:
                d.destination = resolve_destination(
                    self.store, d.recipient_ref, d.channel)
            d.attempts += 1
            try:
                self.sender(d, notification)
            except Exception as exc:  # a mock/real transport failure
                d.status = DeliveryStatus.FAILED
                d.last_error = str(exc) or exc.__class__.__name__
                failed += 1
            else:
                d.status = DeliveryStatus.SENT
                d.sent_at = self.clock()
                d.last_error = None
                sent += 1
            self.store.save_notification_delivery(d)
            processed += 1
        return {"processed": processed, "sent": sent, "failed": failed}
