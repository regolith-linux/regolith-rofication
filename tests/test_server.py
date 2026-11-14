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


class RequestTest(unittest.TestCase):
    """Requests on the socket: the argument is everything after the first colon, and a bad
    notification id is ignored without breaking the server."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'rofi_notification_daemon')
        self.queue = NotificationQueue({}, ())
        for nid, app in ((1, 'App'), (2, 'App:Sub'), (3, 'App')):
            ntf = notification(nid, f'from {app}')
            ntf.application = app
            self.queue.put(ntf)
        self.server = RoficationServer(self.queue, self.path)
        self.server.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, line):
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(self.path)
            client.sendall(line.encode() + b'\n')
            client.shutdown(socket.SHUT_WR)
            reply = b''
            while chunk := client.recv(4096):
                reply += chunk
        return reply.decode()

    def apps(self):
        return sorted(n.application for n in self.queue)

    def test_dela_with_colon_in_application(self):
        self.request('dela:App:Sub')
        self.assertEqual(self.apps(), ['App', 'App'])

    def test_dela_without_colon(self):
        self.request('dela:App')
        self.assertEqual(self.apps(), ['App:Sub'])

    def test_bad_ids_are_ignored(self):
        with mock.patch.object(self.server, 'handle_error') as handle_error:
            for line in ('del:abc', 'del', 'see:', 'delm:', 'delm:1,x'):
                self.request(line)
        handle_error.assert_not_called()
        self.assertEqual(self.apps(), ['App', 'App', 'App:Sub'])
        self.assertEqual(self.request('num'), '3,0')


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
