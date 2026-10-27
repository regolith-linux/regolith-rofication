import json
import os
import threading
import time
from subprocess import CalledProcessError
from typing import Iterable, Iterator, MutableMapping, Mapping, Optional, Tuple
from warnings import warn

from ._notification import Notification, CloseReason, Urgency
from ._util import Event, Resource

ALLOWED_TO_EXPIRE = ('notify-send',)
# Default list of applications that keep at most one notification in the queue.
SINGLE_NOTIFICATION_APPS = ('VLC media player',)

single_notification_apps_resource = Resource(env_name='rofication_notify_single_notification_apps',
                                             xres_name='rofication.notify.single_notification_apps',
                                             default=', '.join(SINGLE_NOTIFICATION_APPS))


def parse_app_list(value: str) -> Tuple[str, ...]:
    """Split a comma-separated list of application names."""
    return tuple(app.strip() for app in value.split(',') if app.strip())


def fetch_single_notification_apps() -> Tuple[str, ...]:
    try:
        return parse_app_list(single_notification_apps_resource.fetch())
    except (OSError, CalledProcessError):
        # no resource getter available: keep the built-in default
        return SINGLE_NOTIFICATION_APPS


class NotificationQueue:
    def __init__(self, mapping: Mapping[int, Notification] = None,
                 single_notification_apps: Iterable[str] = None) -> None:
        self._lock = threading.Lock()
        self._single_notification_apps: Tuple[str, ...] = fetch_single_notification_apps() \
            if single_notification_apps is None else tuple(single_notification_apps)
        self._last_id: int = max(mapping.keys()) + 1 if mapping else 1
        self._mapping: MutableMapping[int, Notification] = {} if mapping is None else dict(mapping)
        self.notification_seen = Event()
        self.notification_closed = Event()

    def __len__(self) -> int:
        return len(self._mapping)

    def __iter__(self) -> Iterator[Notification]:
        return iter(self._mapping.values())

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def save(self, filename: str) -> None:
        try:
            print('Saving notification queue to file')
            with open(filename, 'w') as f:
                # transient notifications by-pass persistence: they don't come back after a restart
                json.dump([ntf for ntf in self._mapping.values() if not ntf.transient], f,
                          default=Notification.asdict)
        except:
            warn('Failed to save notification queue')
            if os.path.exists(filename):
                os.unlink(filename)

    def see(self, nid: int) -> None:
        if nid in self._mapping:
            print(f'Seeing: {nid}')
            notification = self._mapping[nid]
            notification.urgency = Urgency.NORMAL
            self.notification_seen.notify(notification)
            return
        warn(f'Unable to find notification {nid}')

    def remove(self, nid: int) -> None:
        if nid in self._mapping:
            print(f'Removing: {nid}')
            del self._mapping[nid]
            return
        warn(f'Unable to find notification {nid}')

    def remove_all(self, nids: Iterable[int]) -> None:
        for nid in nids:
            self.remove(nid)

    def put(self, notification: Notification) -> None:
        to_replace: Optional[int]
        if notification.application in self._single_notification_apps:
            # replace notification for applications that only allow one
            to_replace = next((ntf.id for ntf in self._mapping.values()
                               if ntf.application == notification.application), None)
        else:
            # cannot have two notifications with the same ID
            to_replace = notification.id if notification.id in self._mapping else None

        if to_replace:
            notification.id = to_replace
            print(f'Replacing: {notification.id}')
            self._mapping[notification.id] = notification
            return

        notification.id = self._last_id
        self._last_id += 1
        print(f'Adding: {notification.id}')
        self._mapping[notification.id] = notification

    def cleanup(self) -> None:
        now = time.time()
        to_remove = [ntf.id for ntf in self._mapping.values()
                     if ntf.deadline and ntf.deadline < now
                     and (ntf.application in ALLOWED_TO_EXPIRE or ntf.transient)]
        if to_remove:
            print(f'Expired: {to_remove}')
            for nid in to_remove:
                self.notification_closed.notify(self._mapping[nid], CloseReason.EXPIRED)
                del self._mapping[nid]

    @classmethod
    def load(cls, filename: str) -> 'NotificationQueue':
        if not os.path.exists(filename):
            print('Creating empty notification queue')
            return cls({})

        try:
            print('Loading notification queue from file')
            with open(filename, 'r') as f:
                # decode only the top-level objects, so each notification keeps its hints as a dict
                notifications = [Notification.make(dct) for dct in json.load(f)]
            mapping = {n.id: n for n in notifications if not n.transient}
        except:
            warn('Failed to load notification queue')
            mapping = {}

        return cls(mapping)
