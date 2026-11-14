from enum import IntEnum
from typing import Sequence, Optional, Mapping, Tuple

class Urgency(IntEnum):
    LOW = 0
    NORMAL = 1
    CRITICAL = 2


class CloseReason(IntEnum):
    EXPIRED = 1
    DISMISSED = 2
    CLOSED = 3
    RESERVED = 4


class Notification:
    # attributes that only live as long as the daemon: they are neither saved nor listed
    RUNTIME_FIELDS = ('expire_timeout',)

    def __init__(self) -> None:
        self.id: Optional[int] = None
        self.deadline: Optional[float] = None
        self.summary: Optional[str] = None
        self.body: Optional[str] = None
        self.application: Optional[str] = None
        self.icon: Optional[str] = None
        self.urgency: Urgency = Urgency.NORMAL
        self.actions: Sequence[str] = ()
        self.hints: Mapping[str, any] = {}
        self.timestamp = None
        # the lifetime the sender asked for, in milliseconds (-1: server default, 0: never expires)
        self.expire_timeout: int = -1

    @property
    def transient(self) -> bool:
        """The sender set the spec's "transient" hint: show it, but keep it out of the saved queue."""
        return isinstance(self.hints, Mapping) and bool(self.hints.get('transient', False))

    @property
    def resident(self) -> bool:
        """The sender set the spec's "resident" hint: invoking an action does not remove it."""
        return isinstance(self.hints, Mapping) and bool(self.hints.get('resident', False))

    @property
    def action_pairs(self) -> Sequence[Tuple[str, str]]:
        """The (key, label) pairs of the actions; a trailing key without a label is ignored."""
        actions = tuple(self.actions or ())
        return tuple(zip(actions[0::2], actions[1::2]))

    def has_action(self, key: str) -> bool:
        return any(action == key for action, _ in self.action_pairs)

    def asdict(self) -> Mapping[str, any]:
        return {field: value for field, value in vars(self).items()
                if value is not None and field not in self.RUNTIME_FIELDS}

    @classmethod
    def make(cls, dct: Mapping[str, any]) -> 'Notification':
        notification: 'Notification' = cls()
        notification.id = dct.get('id')
        notification.deadline = dct.get('deadline')
        notification.summary = dct.get('summary')
        notification.body = dct.get('body')
        notification.application = dct.get('application')
        notification.icon = dct.get('icon')
        notification.urgency = Urgency(dct.get('urgency', Urgency.NORMAL))
        notification.actions = tuple(dct.get('actions', ()))
        notification.hints = dct.get('hints')
        notification.timestamp = dct.get('timestamp', '')
        return notification
