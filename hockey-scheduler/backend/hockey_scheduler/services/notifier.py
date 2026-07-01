"""Feed-notification emission helper (#32).

A tiny helper both the setup and roster services use to append a
:class:`Notification` to the store. Delivery (push/email) is a later concern;
this slice records notifications for an in-app feed with read/unread state.
"""

from ..domain import Notification


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
    return store.add_notification_feed(n)
