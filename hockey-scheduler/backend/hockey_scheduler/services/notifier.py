"""Feed-notification emission helper (#32).

A tiny helper both the setup and roster services use to append a
:class:`Notification` to the store. Emitting a notification also fans it out
to the mock delivery queue (#58); the delivery worker drains that queue
separately.
"""

from ..domain import Notification

from .delivery import enqueue as _enqueue_deliveries


def push(store, clock, kind, audience, title, message,
         audience_ref=None, game_id=None, assignment_id=None) -> Notification:
    n = Notification(
        id=store.next_id("notif_feed"),
        kind=kind,
        audience=audience,
        title=title,
        message=message,
        at=clock(),
        audience_ref=audience_ref,
        game_id=game_id,
        assignment_id=assignment_id,
    )
    store.add_notification_feed(n)
    _enqueue_deliveries(store, n)
    return n
