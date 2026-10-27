import json
import os
import runpy
import socket
import tempfile
import unittest
from unittest import mock

import rofication
from rofication._notification import Notification
from rofication._queue import NotificationQueue
from rofication._server import RoficationServer

DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rofication-daemon')


def notification(nid, summary):
    ntf = Notification()
    ntf.id, ntf.application, ntf.summary, ntf.body = nid, 'test', summary, ''
    return ntf


class SocketCleanupTest(unittest.TestCase):
    """RoficationServer removes its UNIX socket on exit, and only its own."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'rofi_notification_daemon')

    def server(self):
        return RoficationServer(NotificationQueue({}, ()), self.path)

    def test_removes_its_socket(self):
        with self.server():
            self.assertTrue(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_socket_already_removed(self):
        with self.server():
            os.remove(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_keeps_the_socket_of_a_newer_server(self):
        with self.server():
            # A second daemon starts: it removes the socket and binds its own.
            second = self.server()
        self.assertTrue(os.path.exists(self.path), 'the second server lost its socket')
        client = socket.socket(socket.AF_UNIX)
        client.connect(self.path)
        client.close()
        with second:
            pass
        self.assertFalse(os.path.exists(self.path))


class FakeDbusService:
    """Stands in for RoficationDbusService: queues a notification, then lets a
    second daemon replace the socket and stop, as if it had been started by
    mistake, before the first daemon is told to stop."""
    path = None

    def __init__(self, queue):
        self.queue = queue

    def run(self):
        self.queue.put(notification(0, 'kept'))
        with RoficationServer(NotificationQueue({}, ()), self.path):
            pass


class DaemonSavesQueueTest(unittest.TestCase):
    def test_saves_queue_when_its_socket_was_removed(self):
        with tempfile.TemporaryDirectory() as home:
            FakeDbusService.path = os.path.join(home, 'rofi_notification_daemon')
            server = lambda queue: RoficationServer(queue, FakeDbusService.path)
            with mock.patch.dict(os.environ, {'HOME': home}), \
                    mock.patch.object(rofication, 'RoficationDbusService', FakeDbusService), \
                    mock.patch.object(rofication, 'RoficationServer', server):
                runpy.run_path(DAEMON, run_name='__main__')
            with open(os.path.join(home, '.cache', 'rofication', 'notifications.json')) as fp:
                saved = json.load(fp)
        self.assertEqual([n['summary'] for n in saved], ['kept'])


if __name__ == '__main__':
    unittest.main()
