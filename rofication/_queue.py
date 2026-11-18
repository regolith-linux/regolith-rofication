import errno
import json
import os
import threading
import time
from subprocess import CalledProcessError
from typing import Iterable, Iterator, List, MutableMapping, Mapping, Optional, Sequence, Tuple, Union
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


class NotificationQueue:
    def __init__(self, mapping: Mapping[int, Notification] = None,
                 single_notification_apps: Iterable[str] = None, next_id: int = 1) -> None:
        self._lock = threading.Lock()
        self._single_notification_apps: Tuple[str, ...] = fetch_single_notification_apps() \
            if single_notification_apps is None else tuple(single_notification_apps)
        # IDs are never handed out twice, also across restarts (the spec allows reuse only after
        # MAXINT notifications): continue after the highest ID kept in the queue or used before.
        self._last_id: int = max([next_id, *(nid + 1 for nid in mapping or {})])
        self._mapping: MutableMapping[int, Notification] = {} if mapping is None else dict(mapping)
        # when set, the next ID is written there before one is handed out, so that IDs aren't
        # handed out twice even when the daemon is killed before it saves the queue
        self.next_id_file: Optional[str] = None
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
        self.save_next_id(next_id_filename(filename))

    def save_next_id(self, filename: str) -> None:
        try:
            self._write_next_id(filename, self._last_id)
        except OSError:
            warn('Failed to save the next notification ID')

    @staticmethod
    def _write_next_id(filename: str, next_id: int) -> None:
        """Record next_id in filename, or raise OSError. The file is overwritten in place by a single
        write instead of being emptied first, so a daemon killed meanwhile never leaves it empty."""
        data = f'{next_id}\n'.encode('ascii')
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o666)
        try:
            if os.pwrite(fd, data, 0) != len(data):
                raise OSError(errno.EIO, 'short write', filename)
            os.ftruncate(fd, len(data))
        finally:
            os.close(fd)

    @property
    def next_id(self) -> int:
        """The ID the next new notification gets."""
        return self._last_id

    def see(self, nid: int) -> None:
        if nid in self._mapping:
            print(f'Seeing: {nid}')
            notification = self._mapping[nid]
            notification.urgency = Urgency.NORMAL
            self.notification_seen.notify(notification)
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
        """Add or replace a notification. Raises OSError, leaving the queue as it was, when a new ID
        is needed and the next ID can't be recorded."""
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

        nid = self._last_id
        if self.next_id_file:
            # the record must cover an ID before it is handed out
            self._write_next_id(self.next_id_file, nid + 1)
        notification.id = nid
        self._last_id = nid + 1
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
    def _read(cls, filename: str) -> Tuple[Sequence[Notification], int]:
        try:
            with open(next_id_filename(filename), 'r') as f:
                next_id = int(f.read())
        except (OSError, ValueError):
            next_id = 1

        if not os.path.exists(filename):
            print('Creating empty notification queue')
            return [], next_id

        try:
            print('Loading notification queue from file')
            with open(filename, 'r') as f:
                # decode only the top-level objects, so each notification keeps its hints as a dict
                notifications = [Notification.make(dct) for dct in json.load(f)]
            notifications = [n for n in notifications if not n.transient]
        except:
            warn('Failed to load notification queue')
            notifications = []

        return notifications, next_id

    @classmethod
    def load(cls, filename: str) -> 'NotificationQueue':
        notifications, next_id = cls._read(filename)
        return cls({n.id: n for n in notifications}, next_id=next_id)

    @classmethod
    def load_all(cls, sources: Sequence[Union[str, Tuple[Sequence[Notification], int]]]) -> 'NotificationQueue':
        """Merge several queues: saved queues, given by file name, and queues held in memory by daemons
        this one replaces, given as their notifications and next ID. Earlier sources keep their IDs; a
        notification whose ID is taken gets a new one. A file named more than once is read once."""
        loaded: List[Tuple[Sequence[Notification], int]] = []
        seen = set()
        for source in sources:
            if isinstance(source, str):
                if os.path.realpath(source) in seen:
                    continue
                seen.add(os.path.realpath(source))
                source = cls._read(source)
            loaded.append(source)
        if len(loaded) == 1:
            notifications, next_id = loaded[0]
            return cls({n.id: n for n in notifications}, next_id=next_id)

        mapping: MutableMapping[int, Notification] = {}
        renumber = []
        for notifications, next_id in loaded:
            for notification in notifications:
                if notification.id in mapping:
                    renumber.append(notification)
                else:
                    mapping[notification.id] = notification
        queue = cls(dict(sorted(mapping.items())), next_id=max([1, *(next_id for _, next_id in loaded)]))
        for notification in renumber:
            notification.id = queue._last_id
            queue._last_id += 1
            queue._mapping[notification.id] = notification
        return queue
