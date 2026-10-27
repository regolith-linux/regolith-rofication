import os
import tempfile
import time
import unittest

from rofication._client import RoficationClient
from rofication._dbus import RoficationDbusObject
from rofication._notification import CloseReason, Notification
from rofication._queue import NotificationQueue
from rofication._server import RoficationServer


def notification(application, summary='s', nid=0):
    ntf = Notification()
    ntf.id = nid
    ntf.application = application
    ntf.summary = summary
    ntf.body = ''
    return ntf


class CloseReasonTest(unittest.TestCase):
    """Every notification that leaves the queue is reported through
    notification_closed, which the D-Bus service emits as NotificationClosed."""

    def setUp(self):
        self.queue = NotificationQueue({}, single_notification_apps=())
        self.closed = []
        self.queue.notification_closed += lambda ntf, reason: self.closed.append((ntf.id, reason))

    def put(self, application, summary='s', nid=0):
        ntf = notification(application, summary, nid)
        self.queue.put(ntf)
        return ntf.id

    def test_remove_reports_dismissed_by_default(self):
        nid = self.put('app')
        self.queue.remove(nid)
        self.assertEqual(self.closed, [(nid, CloseReason.DISMISSED)])
        self.assertEqual(len(self.queue), 0)

    def test_remove_reports_given_reason(self):
        nid = self.put('app')
        self.queue.remove(nid, CloseReason.CLOSED)
        self.assertEqual(self.closed, [(nid, CloseReason.CLOSED)])

    def test_remove_unknown_id_reports_nothing(self):
        self.put('app')
        self.queue.remove(999)
        self.assertEqual(self.closed, [])
        self.assertEqual(len(self.queue), 1)

    def test_remove_all_reports_each(self):
        ids = [self.put('app', str(i)) for i in range(3)]
        self.queue.remove_all(ids[:2])
        self.assertEqual(self.closed, [(ids[0], CloseReason.DISMISSED), (ids[1], CloseReason.DISMISSED)])

    def test_replacing_reports_nothing(self):
        nid = self.put('app', 'first')
        self.put('app', 'second', nid=nid)
        self.assertEqual(self.closed, [])
        self.assertEqual([n.summary for n in self.queue], ['second'])

    def test_expiry_reports_expired(self):
        ntf = notification('notify-send')
        ntf.deadline = time.time() - 1
        self.queue.put(ntf)
        self.queue.cleanup()
        self.assertEqual(self.closed, [(ntf.id, CloseReason.EXPIRED)])

    def test_close_notification_method_reports_closed(self):
        nid = self.put('app')

        class Service:
            _queue = self.queue

        RoficationDbusObject.CloseNotification(Service(), nid)
        self.assertEqual(self.closed, [(nid, CloseReason.CLOSED)])


class SocketDismissTest(unittest.TestCase):
    """Dismissing from rofication-gui (the UNIX socket commands) reports
    reason 2, dismissed by the user."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = os.path.join(self._dir.name, 'sock')
        self.queue = NotificationQueue({}, single_notification_apps=())
        self.closed = []
        self.queue.notification_closed += lambda ntf, reason: self.closed.append((ntf.id, reason))
        self.server = RoficationServer(self.queue, server_address=path)
        self.server.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.client = RoficationClient(unix_socket=path)

    def put(self, application):
        ntf = notification(application)
        with self.queue.lock:
            self.queue.put(ntf)
        return ntf.id

    def wait_for(self, count):
        deadline = time.time() + 5
        while len(self.closed) < count and time.time() < deadline:
            time.sleep(0.01)

    def test_delete(self):
        nid = self.put('app')
        self.client.delete(nid)
        self.wait_for(1)
        self.assertEqual(self.closed, [(nid, CloseReason.DISMISSED)])

    def test_delete_multi(self):
        ids = [self.put('app') for _ in range(3)]
        self.client.delete_multi(f'{ids[0]},{ids[2]}')
        self.wait_for(2)
        self.assertEqual(sorted(self.closed), [(ids[0], CloseReason.DISMISSED), (ids[2], CloseReason.DISMISSED)])

    def test_delete_all_of_application(self):
        a1, b1, a2 = self.put('a'), self.put('b'), self.put('a')
        self.client.delete_all('a')
        self.wait_for(2)
        self.assertEqual(sorted(self.closed), [(a1, CloseReason.DISMISSED), (a2, CloseReason.DISMISSED)])
        self.assertEqual([n.id for n in self.queue], [b1])


if __name__ == '__main__':
    unittest.main()
