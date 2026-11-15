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


def next_id_filename(filename: str) -> str:
    """The file next to the queue file that keeps the next notification ID."""
    return os.path.splitext(filename)[0] + '.next-id'


def do_not_disturb_filename(filename: str) -> str:
    """The file in the queue's directory that keeps the do-not-disturb mode."""
    return os.path.join(os.path.dirname(filename), 'do-not-disturb')


def load_do_not_disturb(filename: str) -> bool:
    try:
        with open(do_not_disturb_filename(filename), 'r') as f:
            return f.read().strip() == 'on'
    except OSError:
        return False


class NotificationQueue:
    def __init__(self, mapping: Mapping[int, Notification] = None,
                 single_notification_apps: Iterable[str] = None, next_id: int = 1,
                 do_not_disturb: bool = False) -> None:
        self._lock = threading.Lock()
        self._single_notification_apps: Tuple[str, ...] = fetch_single_notification_apps() \
            if single_notification_apps is None else tuple(single_notification_apps)
        # IDs are never handed out twice, also across restarts (the spec allows reuse only after
        # MAXINT notifications): continue after the highest ID kept in the queue or used before.
        self._last_id: int = max([next_id, *(nid + 1 for nid in mapping or {})])
        self._mapping: MutableMapping[int, Notification] = {} if mapping is None else dict(mapping)
        self._do_not_disturb: bool = bool(do_not_disturb)
        # notification added or replaced by a sender
        self.notification_put = Event()
        self.notification_seen = Event()
        self.notification_closed = Event()
        # the user invoked one of the notification's actions
        self.notification_action = Event()
        self.do_not_disturb_changed = Event()

    def __len__(self) -> int:
        return len(self._mapping)

    def __iter__(self) -> Iterator[Notification]:
        return iter(self._mapping.values())

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def get(self, nid: int) -> Optional[Notification]:
        return self._mapping.get(nid)

    @property
    def do_not_disturb(self) -> bool:
        return self._do_not_disturb

    def set_do_not_disturb(self, enabled: bool) -> bool:
        """Switch do-not-disturb on or off; returns whether the mode changed."""
        enabled = bool(enabled)
        if enabled == self._do_not_disturb:
            return False
        print(f'Do not disturb: {"on" if enabled else "off"}')
        self._do_not_disturb = enabled
        self.do_not_disturb_changed.notify(enabled)
        return True

    def save_do_not_disturb(self, filename: str) -> None:
        """Keep the do-not-disturb mode next to the queue file, without touching the queue file."""
        path = do_not_disturb_filename(filename)
        try:
            with open(path + '.tmp', 'w') as f:
                f.write('on\n' if self._do_not_disturb else 'off\n')
            os.replace(path + '.tmp', path)
        except OSError:
            warn('Failed to save the do-not-disturb mode')

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
        try:
            with open(next_id_filename(filename), 'w') as f:
                f.write(f'{self._last_id}\n')
        except OSError:
            warn('Failed to save the next notification ID')
        self.save_do_not_disturb(filename)

    def see(self, nid: int) -> None:
        if nid in self._mapping:
            print(f'Seeing: {nid}')
            notification = self._mapping[nid]
            notification.urgency = Urgency.NORMAL
            self.notification_seen.notify(notification)
            return
        warn(f'Unable to find notification {nid}')

    def invoke(self, nid: int, action_key: str) -> None:
        if nid in self._mapping:
            print(f'Invoking {action_key!r} on: {nid}')
            self.notification_action.notify(self._mapping[nid], action_key)
            return
        warn(f'Unable to find notification {nid}')

    def remove(self, nid: int, reason: CloseReason = CloseReason.DISMISSED) -> None:
        if nid in self._mapping:
            print(f'Removing: {nid}')
            notification = self._mapping.pop(nid)
            self.notification_closed.notify(notification, reason)
            return
        warn(f'Unable to find notification {nid}')

    def remove_all(self, nids: Iterable[int], reason: CloseReason = CloseReason.DISMISSED) -> None:
        for nid in nids:
            self.remove(nid, reason)

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
        else:
            notification.id = self._last_id
            self._last_id += 1
            print(f'Adding: {notification.id}')
            self._mapping[notification.id] = notification
        self.notification_put.notify(notification)

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
        try:
            with open(next_id_filename(filename), 'r') as f:
                next_id = int(f.read())
        except (OSError, ValueError):
            next_id = 1

        do_not_disturb = load_do_not_disturb(filename)

        if not os.path.exists(filename):
            print('Creating empty notification queue')
            return cls({}, next_id=next_id, do_not_disturb=do_not_disturb)

        try:
            print('Loading notification queue from file')
            with open(filename, 'r') as f:
                # decode only the top-level objects, so each notification keeps its hints as a dict
                notifications = [Notification.make(dct) for dct in json.load(f)]
            mapping = {n.id: n for n in notifications if not n.transient}
        except:
            warn('Failed to load notification queue')
            mapping = {}

        return cls(mapping, next_id=next_id, do_not_disturb=do_not_disturb)
