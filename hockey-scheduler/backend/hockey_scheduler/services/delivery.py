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
    """
    aud = notification.audience
    ref = notification.audience_ref
    if aud == NotificationAudience.OFFICIAL and ref:
        return "official:" + ref
    if aud == NotificationAudience.COACH and ref:
        return "team:" + ref
    if aud == NotificationAudience.SCHEDULER:
        return "scheduler"
    return "public"


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
    """The real stored contact for (recipient, channel), else a placeholder (#60).

    Operators can register a real destination via the contact registry; when
    none exists we fall back to the synthesized ``.invalid`` placeholder so a
    delivery always has somewhere (fictional) to go.
    """
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
    """The default sender: a no-op stand-in that always "delivers".

    Real transports (email/push) replace this later. Raising an exception
    signals a failed send, which the worker records and retries.
    """
    return None


class DeliveryWorker:
    """Drains pending notification deliveries through an injected sender."""

    def __init__(self, store, clock, sender=mock_sender,
                 max_attempts: int = MAX_ATTEMPTS):
        self.store = store
        self.clock = clock
        self.sender = sender
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
