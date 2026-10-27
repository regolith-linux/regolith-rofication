import json
import os
import tempfile
import time
import unittest

from rofication._dbus import RoficationDbusObject
from rofication._notification import CloseReason, Notification
from rofication._queue import NotificationQueue


def notification(application, summary='s', hints=None, deadline=None):
    ntf = Notification()
    ntf.id = 0
    ntf.application = application
    ntf.summary = summary
    ntf.body = ''
    ntf.hints = {} if hints is None else hints
    ntf.deadline = deadline
    return ntf


class PersistenceTest(unittest.TestCase):
    """The queue is the persistence the spec describes: notifications are kept,
    also across restarts, until dismissed or closed, except transient ones."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'notifications.json')
        self.queue = NotificationQueue({}, single_notification_apps=())

    def restart(self):
        self.queue.save(self.path)
        return NotificationQueue.load(self.path)

    def test_capabilities_advertise_persistence(self):
        self.assertEqual(set(RoficationDbusObject.GetCapabilities(None)), {'actions', 'body', 'persistence'})

    def test_transient_shown_but_not_saved(self):
        self.queue.put(notification('app', 'kept'))
        self.queue.put(notification('app', 'transient', hints={'transient': True}))
        self.assertEqual(sorted(n.summary for n in self.queue), ['kept', 'transient'])
        self.assertEqual([n.summary for n in self.restart()], ['kept'])

    def test_transient_false_is_saved(self):
        self.queue.put(notification('app', 'not transient', hints={'transient': False}))
        self.assertEqual([n.summary for n in self.restart()], ['not transient'])

    def test_transient_byte_value(self):
        # some senders use a byte or an integer instead of a boolean
        self.queue.put(notification('app', 'transient', hints={'transient': 1}))
        self.assertEqual(list(self.restart()), [])

    def test_restored_notifications_keep_hints(self):
        self.queue.put(notification('app', 'kept', hints={'category': 'email.arrived', 'urgency': 2}))
        restored = list(self.restart())
        self.assertEqual(restored[0].hints, {'category': 'email.arrived', 'urgency': 2})

    def test_transient_from_older_file_is_dropped(self):
        with open(self.path, 'w') as fp:
            json.dump([{'id': 3, 'application': 'app', 'summary': 'old transient', 'body': '',
                        'hints': {'transient': True}},
                       {'id': 4, 'application': 'app', 'summary': 'old', 'body': '', 'hints': {}}], fp)
        self.assertEqual([(n.id, n.summary) for n in NotificationQueue.load(self.path)], [(4, 'old')])

    def test_transient_expires_at_its_timeout(self):
        closed = []
        self.queue.notification_closed += lambda ntf, reason: closed.append((ntf.summary, reason))
        past = time.time() - 1
        self.queue.put(notification('app', 'transient', hints={'transient': True}, deadline=past))
        self.queue.put(notification('app', 'transient, no timeout', hints={'transient': True}))
        self.queue.put(notification('app', 'regular', deadline=past))
        self.queue.cleanup()
        self.assertEqual(closed, [('transient', CloseReason.EXPIRED)])
        self.assertEqual(sorted(n.summary for n in self.queue), ['regular', 'transient, no timeout'])


if __name__ == '__main__':
    unittest.main()
