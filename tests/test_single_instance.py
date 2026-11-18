import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import rofication
from rofication import _runtime
from rofication._client import RoficationClient
from rofication._notification import Notification
from rofication._queue import NotificationQueue
from rofication._server import RoficationServer

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daemon_stub.py')
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(rofication.__file__)))
TIMEOUT = 10


def request(path, line):
    with socket.socket(socket.AF_UNIX) as sck:
        sck.settimeout(TIMEOUT)
        sck.connect(path)
        sck.sendall(line.encode() + b'\n')
        sck.shutdown(socket.SHUT_WR)
        reply = b''
        while chunk := sck.recv(4096):
            reply += chunk
    return reply.decode()


class DaemonTestCase(unittest.TestCase):
    """Runs rofication-daemon processes with a stand-in for the D-Bus service."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.home = os.path.join(self._dir.name, 'home')
        self.runtime = os.path.join(self._dir.name, 'run')
        os.mkdir(self.home)
        os.mkdir(self.runtime, 0o700)
        self.socket = os.path.join(self.runtime, 'rofi_notification_daemon')
        self.legacy = os.path.join(self._dir.name, 'rofi_notification_daemon')
        self.queue_file = os.path.join(self.home, '.cache', 'rofication', 'notifications.json')
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': self.home,
                    'XDG_RUNTIME_DIR': self.runtime, 'PYTHONPATH': PACKAGE_DIR,
                    'STUB_LEGACY': self.legacy}
        self._procs = []

    def tearDown(self):
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def start(self, *args, notify=(), env=None, umask=None):
        stderr = tempfile.TemporaryFile(dir=self._dir.name)
        self.addCleanup(stderr.close)
        proc = subprocess.Popen([sys.executable, STUB, *args], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=stderr,
                                env={**self.env, 'STUB_NOTIFY': ','.join(notify), **(env or {})},
                                preexec_fn=None if umask is None else lambda: os.umask(umask))
        proc.stderr_file = stderr
        self._procs.append(proc)
        return proc

    @staticmethod
    def stderr(proc):
        proc.stderr_file.seek(0)
        return proc.stderr_file.read().decode()

    def wait_count(self, count, path=None):
        """Wait for a daemon to answer at path with count notifications."""
        deadline = time.monotonic() + TIMEOUT
        while True:
            try:
                if request(path or self.socket, 'num').split(',')[0] == str(count):
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                self.fail(f'no daemon with {count} notifications at {path or self.socket}')
            time.sleep(0.02)

    def wait_link(self):
        """Wait for the old shared path to be a link, and return where it leads. A daemon links it
        only after its socket answers, and deciding whether an old socket there is still in use can
        take a while, so a daemon that answers may not have linked it yet."""
        deadline = time.monotonic() + TIMEOUT
        while not os.path.islink(self.legacy):
            if time.monotonic() > deadline:
                self.fail(f'{self.legacy} was not linked')
            time.sleep(0.02)
        return os.readlink(self.legacy)

    def listing(self, path=None):
        return {n['summary']: n for n in json.loads(request(path or self.socket, 'list'))}

    def stop(self, proc):
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(TIMEOUT), 0, self.stderr(proc))

    def saved(self):
        with open(self.queue_file) as fp:
            return [n['summary'] for n in json.load(fp)]


class SocketTest(DaemonTestCase):
    def test_socket_is_private_and_the_old_path_links_to_it(self):
        daemon = self.start(notify=['one'], umask=0o777)
        self.wait_count(1)
        self.assertEqual(stat.S_IMODE(os.lstat(self.socket).st_mode), 0o600)
        self.assertEqual(self.wait_link(), self.socket)
        self.assertEqual(request(self.legacy, 'num'), '1,0')
        self.stop(daemon)
        self.assertFalse(os.path.lexists(self.socket))
        self.assertFalse(os.path.lexists(self.legacy))
        self.assertEqual(self.saved(), ['one'])

    def test_old_path_that_is_not_a_link_or_socket_is_left_alone(self):
        with open(self.legacy, 'w') as fp:
            fp.write('mine')
        daemon = self.start(notify=['one'])
        self.wait_count(1)
        self.stop(daemon)
        with open(self.legacy) as fp:
            self.assertEqual(fp.read(), 'mine')
        self.assertEqual(self.stderr(daemon).count(self.legacy), 1)

    def _leave_socket(self, path, mode=None):
        """A socket file of this user that nothing listens on anymore."""
        with socket.socket(socket.AF_UNIX) as sck:
            sck.bind(path)
        if mode is not None:
            os.chmod(path, mode)

    def test_old_socket_without_listener_is_replaced(self):
        for mode in (None, 0):
            with self.subTest(mode=mode):
                self._leave_socket(self.legacy, mode)
                daemon = self.start()
                self.wait_count(0)
                self.assertEqual(self.wait_link(), self.socket)
                self.stop(daemon)
                self.assertFalse(os.path.lexists(self.legacy))

    def test_old_socket_with_listener_is_left_alone(self):
        with socket.socket(socket.AF_UNIX) as other:
            other.bind(self.legacy)
            other.listen(1)
            inode = os.lstat(self.legacy).st_ino
            daemon = self.start()
            self.wait_count(0)
            self.assertEqual(os.lstat(self.legacy).st_ino, inode)
            self.stop(daemon)
            self.assertEqual(os.lstat(self.legacy).st_ino, inode)
            self.assertEqual(self.stderr(daemon).count(self.legacy), 1)

    def test_lock_file_that_is_not_a_regular_file(self):
        lock = self.socket + '.lock'
        os.mkdir(lock)
        daemon = self.start()
        self.assertEqual(daemon.wait(TIMEOUT), 1)
        self.assertIn(lock, self.stderr(daemon))
        self.assertFalse(os.path.lexists(self.socket))

    def test_silent_client_does_not_hold_up_stopping(self):
        daemon = self.start(notify=['one'])
        self.wait_count(1)
        with socket.socket(socket.AF_UNIX) as silent:
            silent.connect(self.socket)
            time.sleep(0.2)
            self.stop(daemon)
        self.assertEqual(self.saved(), ['one'])

    def test_old_path_repointed_meanwhile_is_kept(self):
        daemon = self.start()
        self.wait_count(0)
        self.wait_link()
        os.unlink(self.legacy)
        os.symlink('/nonexistent', self.legacy)
        self.stop(daemon)
        self.assertEqual(os.readlink(self.legacy), '/nonexistent')

    def test_runtime_dir_that_does_not_exist(self):
        missing = os.path.join(self._dir.name, 'missing')
        daemon = self.start(env={'XDG_RUNTIME_DIR': missing})
        self.assertEqual(daemon.wait(TIMEOUT), 1)
        self.assertIn(missing, self.stderr(daemon))
        self.assertFalse(os.path.lexists(missing))
        self.assertFalse(os.path.lexists(os.path.join(self.home, '.cache')))


class FallbackRuntimeDirTest(DaemonTestCase):
    """Without XDG_RUNTIME_DIR, the daemon serves from a directory of its own, mode 0700."""

    def setUp(self):
        super().setUp()
        del self.env['XDG_RUNTIME_DIR']
        self.fallback = os.path.join(self._dir.name, 'rofication-uid')
        self.env['STUB_FALLBACK'] = self.fallback
        self.socket = os.path.join(self.fallback, 'rofi_notification_daemon')

    def test_created_with_fixed_modes(self):
        daemon = self.start(umask=0o777)
        self.wait_count(0)
        self.assertEqual(stat.S_IMODE(os.lstat(self.fallback).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.socket).st_mode), 0o600)
        self.stop(daemon)

    def test_existing_directory_gets_mode_0700(self):
        os.mkdir(self.fallback)
        os.chmod(self.fallback, 0o755)
        daemon = self.start()
        self.wait_count(0)
        self.assertEqual(stat.S_IMODE(os.lstat(self.fallback).st_mode), 0o700)
        self.stop(daemon)

    def test_symbolic_link_is_refused(self):
        target = os.path.join(self._dir.name, 'elsewhere')
        os.mkdir(target)
        os.symlink(target, self.fallback)
        daemon = self.start()
        self.assertEqual(daemon.wait(TIMEOUT), 1)
        self.assertIn(self.fallback, self.stderr(daemon))
        self.assertEqual(os.listdir(target), [])


class SingleInstanceTest(DaemonTestCase):
    def test_second_daemon_exits_and_changes_nothing(self):
        first = self.start(notify=['kept'])
        self.wait_count(1)
        self.assertEqual(self.wait_link(), self.socket)
        inode = os.lstat(self.socket).st_ino
        for env in ({}, {'XDG_RUNTIME_DIR': self._dir.name}):
            with self.subTest(env=env):
                second = self.start(notify=['lost'], env=env)
                self.assertEqual(second.wait(TIMEOUT), 1)
                self.assertIn('already running', self.stderr(second))
                self.assertEqual(os.lstat(self.socket).st_ino, inode)
                self.assertEqual(os.readlink(self.legacy), self.socket)
                self.assertEqual(request(self.legacy, 'num'), '1,0')
        self.stop(first)
        self.assertEqual(self.saved(), ['kept'])

    def test_replace_takes_over_everything_held(self):
        first = self.start(notify=['one', 'transient two'])
        self.wait_count(2)
        second = self.start('--replace', notify=['three'])
        self.assertEqual(first.wait(TIMEOUT), 0, self.stderr(first))
        self.wait_count(3)
        held = self.listing()
        self.assertEqual({summary: n['id'] for summary, n in held.items()},
                         {'one': 1, 'transient two': 2, 'three': 3})
        self.assertTrue(held['transient two']['hints']['transient'])
        self.assertEqual(self.wait_link(), self.socket)
        self.stop(second)
        self.assertEqual(self.saved(), ['one', 'three'])

    def test_killed_daemon_does_not_block_or_reset_ids(self):
        first = self.start(notify=['one', 'two'])
        self.wait_count(2)
        first.send_signal(signal.SIGKILL)
        first.wait(TIMEOUT)
        second = self.start(notify=['three'])
        self.wait_count(1)
        self.assertEqual(self.listing()['three']['id'], 3)
        self.assertEqual(self.wait_link(), self.socket)
        self.stop(second)


class ClientTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'rofi_notification_daemon')

    def test_server_socket_mode_ignores_umask(self):
        umask = os.umask(0o777)
        try:
            server = RoficationServer(NotificationQueue({}, ()), self.path)
        finally:
            os.umask(umask)
        with server:
            self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)

    def test_socket_of_another_user_counts_as_no_daemon(self):
        with RoficationServer(NotificationQueue({}, ()), self.path) as server:
            server.start()
            try:
                self.assertEqual(tuple(RoficationClient(unix_socket=self.path).count()), (0, 0))
                other = lambda sck: (os.getpid(), os.getuid() + 1, os.getgid())
                with mock.patch.object(_runtime, 'peer_credentials', other):
                    with self.assertRaises(ConnectionRefusedError):
                        RoficationClient(unix_socket=self.path).count()
            finally:
                server.shutdown()

    def test_malformed_count_reply(self):
        for reply in (b'', b'3', b'x,y'):
            with self.subTest(reply=reply), socket.socket(socket.AF_UNIX) as listener:
                if os.path.lexists(self.path):
                    os.unlink(self.path)
                listener.bind(self.path)
                listener.listen(1)

                def answer():
                    sck, _ = listener.accept()
                    with sck:
                        sck.recv(64)
                        sck.sendall(reply)

                thread = threading.Thread(target=answer)
                thread.start()
                with self.assertRaises(ValueError):
                    RoficationClient(unix_socket=self.path).count()
                thread.join(TIMEOUT)


class NextIdTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.queue = NotificationQueue({}, ())

    @staticmethod
    def notification():
        ntf = Notification()
        ntf.application, ntf.summary, ntf.body = 'app', 's', ''
        return ntf

    def test_recorded_before_it_is_handed_out(self):
        self.queue.next_id_file = os.path.join(self._dir.name, 'notifications.next-id')
        self.queue.put(self.notification())
        with open(self.queue.next_id_file) as fp:
            self.assertEqual(fp.read(), '2\n')

    def test_not_handed_out_when_it_cannot_be_recorded(self):
        self.queue.next_id_file = self._dir.name  # a directory can't be written as a file
        with self.assertRaises(OSError):
            self.queue.put(self.notification())
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(self.queue.next_id, 1)


if __name__ == '__main__':
    unittest.main()
