import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from rofication._notification import Notification
from rofication._queue import NotificationQueue

XRES_NAME = 'i3xrocks.notify.single_notification_apps'
ENV_NAME = 'i3xrocks_notify_single_notification_apps'


def notification(application, summary='s', nid=0):
    ntf = Notification()
    ntf.id = nid
    ntf.application = application
    ntf.summary = summary
    ntf.body = ''
    return ntf


class SingleNotificationAppsTest(unittest.TestCase):
    """NotificationQueue.put() with the single-notification app list read
    through xrescat (stubbed on PATH) or its environment variable."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.bin = self._dir.name

    def stub_xrescat(self, body):
        path = os.path.join(self.bin, 'xrescat')
        with open(path, 'w') as fp:
            fp.write('#!/bin/sh\n' + body + '\n')
        os.chmod(path, stat.S_IRWXU)

    def stub_resource(self, value):
        # print the configured value for our key, the default for any other
        self.stub_xrescat(f'if [ "$1" = "{XRES_NAME}" ]; then printf %s "{value}"; else printf %s "$2"; fi')

    def make_queue(self, extra_env=None, **kwargs):
        env = {'PATH': self.bin}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env, clear=True):
            return NotificationQueue(**kwargs)

    def count(self, queue, application):
        return sum(1 for ntf in queue if ntf.application == application)

    def put_twice(self, queue, application):
        queue.put(notification(application, 'first'))
        queue.put(notification(application, 'second'))

    def assert_single(self, queue, application):
        self.put_twice(queue, application)
        self.assertEqual(self.count(queue, application), 1, application)
        self.assertEqual([n.summary for n in queue if n.application == application], ['second'])

    def assert_stacked(self, queue, application):
        self.put_twice(queue, application)
        self.assertEqual(self.count(queue, application), 2, application)

    def test_default_without_resource_getter(self):
        queue = self.make_queue()
        self.assert_single(queue, 'VLC media player')
        self.assert_stacked(queue, 'Spotify')

    def test_default_when_resource_unset(self):
        self.stub_xrescat('printf %s "$2"')
        queue = self.make_queue()
        self.assert_single(queue, 'VLC media player')
        self.assert_stacked(queue, 'Spotify')

    def test_default_when_getter_fails(self):
        self.stub_xrescat('echo "cannot open display" >&2; exit 1')
        queue = self.make_queue()
        self.assert_single(queue, 'VLC media player')
        self.assert_stacked(queue, 'Spotify')

    def test_resource_replaces_default_list(self):
        self.stub_resource('Spotify, Some App ')
        queue = self.make_queue()
        self.assert_single(queue, 'Spotify')
        self.assert_single(queue, 'Some App')
        self.assert_stacked(queue, 'VLC media player')

    def test_resource_lookup_uses_key_and_default(self):
        self.stub_xrescat('printf "%s|%s" "$1" "$2" > "${0%/*}/args"; printf %s "$2"')
        self.make_queue()
        with open(os.path.join(self.bin, 'args')) as fp:
            self.assertEqual(fp.read(), f'{XRES_NAME}|VLC media player')

    def test_empty_resource_disables_replacement(self):
        self.stub_resource('')
        queue = self.make_queue()
        self.assert_stacked(queue, 'VLC media player')

    def test_environment_variable_wins(self):
        self.stub_resource('Spotify')
        queue = self.make_queue(extra_env={ENV_NAME: 'Firefox,VLC media player'})
        self.assert_single(queue, 'Firefox')
        self.assert_single(queue, 'VLC media player')
        self.assert_stacked(queue, 'Spotify')

    def test_explicit_list(self):
        self.stub_resource('Spotify')
        queue = self.make_queue(single_notification_apps=('Firefox',))
        self.assert_single(queue, 'Firefox')
        self.assert_stacked(queue, 'Spotify')

    def test_same_id_still_replaced_for_other_apps(self):
        queue = self.make_queue()
        queue.put(notification('Spotify', 'first'))
        nid = next(iter(queue)).id
        queue.put(notification('Spotify', 'second', nid=nid))
        self.assertEqual([n.summary for n in queue], ['second'])

    def test_load_reads_resource(self):
        self.stub_resource('Spotify')
        path = os.path.join(self._dir.name, 'notifications.json')
        with open(path, 'w') as fp:
            json.dump([{'id': 4, 'application': 'Spotify', 'summary': 'old', 'body': ''}], fp)
        with mock.patch.dict(os.environ, {'PATH': self.bin}, clear=True):
            queue = NotificationQueue.load(path)
        queue.put(notification('Spotify', 'new'))
        self.assertEqual([(n.id, n.summary) for n in queue], [(4, 'new')])


if __name__ == '__main__':
    unittest.main()
