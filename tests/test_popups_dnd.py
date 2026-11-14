import os
import socket
import tempfile
import sys
import threading
import types
import unittest
from unittest import mock

from gi.repository import GLib

from rofication._client import RoficationClient
from rofication._notification import Notification, Urgency
from rofication._popups import PopupSettings, RoficationPopups, parse_min_urgency, parse_whole_number
from rofication._queue import NotificationQueue
from rofication._server import RoficationServer


def notification(urgency=Urgency.NORMAL, expire_timeout=-1, actions=(), hints=None):
    ntf = Notification()
    ntf.application = 'app'
    ntf.summary = 'summary'
    ntf.body = 'body'
    ntf.urgency = urgency
    ntf.expire_timeout = expire_timeout
    ntf.actions = tuple(actions)
    ntf.hints = hints or {}
    return ntf


class SettingsTest(unittest.TestCase):
    def test_whole_numbers(self):
        self.assertEqual(parse_whole_number('2', 1), 2)
        self.assertEqual(parse_whole_number(' 0 ', 0), 0)
        for value in ('0', '-1', '1.5', 'not-a-number', '', 'inf'):
            self.assertIsNone(parse_whole_number(value, 1), value)

    def test_min_urgency(self):
        self.assertEqual(parse_min_urgency('low'), Urgency.LOW)
        self.assertEqual(parse_min_urgency('critical'), Urgency.CRITICAL)
        self.assertEqual(parse_min_urgency(' Normal '), Urgency.NORMAL)
        self.assertIsNone(parse_min_urgency('none'))
        # unknown values mean the default, none
        self.assertIsNone(parse_min_urgency('bogus'))
        self.assertIsNone(parse_min_urgency(''))

    def test_pop_ups_are_off_by_default(self):
        with mock.patch.dict(os.environ, {'PATH': ''}, clear=True):
            self.assertIsNone(PopupSettings.fetch().min_urgency)
        self.assertIsNone(PopupSettings().min_urgency)

    def test_environment_overrides_and_invalid_values_mean_default(self):
        env = {'rofication_popup_max_visible': '0', 'rofication_popup_timeout_low': '1200',
               'rofication_popup_timeout_normal': '-5', 'rofication_popup_min_urgency': 'low',
               'PATH': ''}
        with mock.patch.dict(os.environ, env, clear=True):
            settings = PopupSettings.fetch()
        self.assertEqual((settings.max_visible, settings.timeout_low, settings.timeout_normal, settings.min_urgency),
                         (3, 1200, 8000, Urgency.LOW))

    def test_shows(self):
        self.assertFalse(PopupSettings().shows(notification(Urgency.CRITICAL)))
        settings = PopupSettings(min_urgency=Urgency.NORMAL)
        self.assertFalse(settings.shows(notification(Urgency.LOW)))
        self.assertTrue(settings.shows(notification(Urgency.NORMAL)))
        self.assertFalse(settings.shows(notification(Urgency.NORMAL), do_not_disturb=True))
        self.assertTrue(settings.shows(notification(Urgency.CRITICAL), do_not_disturb=True))
        self.assertFalse(PopupSettings(min_urgency=None).shows(notification(Urgency.CRITICAL)))

    def test_lifetime(self):
        settings = PopupSettings(timeout_low=5000, timeout_normal=8000)
        self.assertEqual(settings.lifetime(notification(expire_timeout=1500)), 1500)
        self.assertEqual(settings.lifetime(notification(expire_timeout=0)), 0)
        self.assertEqual(settings.lifetime(notification(Urgency.LOW)), 5000)
        self.assertEqual(settings.lifetime(notification(Urgency.NORMAL)), 8000)
        self.assertEqual(settings.lifetime(notification(Urgency.CRITICAL, expire_timeout=1000)), 0)


class StockInstallTest(unittest.TestCase):
    """A stock install shows no pop-up; setting rofication.popup.min_urgency brings them back."""

    def setUp(self):
        # stands in for the GTK side, so that no display is needed
        self.window_module = types.ModuleType('rofication._popup_window')
        self.window_module.init_display = mock.Mock()
        self.window_module.PopupWindow = mock.Mock(side_effect=self.make_popup)
        patcher = mock.patch.dict(sys.modules, {'rofication._popup_window': self.window_module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.shown = []

    def make_popup(self, ntf, settings, on_press, on_action):
        popup = mock.Mock()
        popup.notification = ntf
        popup.height.return_value = 50
        popup.show_all.side_effect = lambda: self.shown.append(ntf.summary)
        return popup

    def notify(self, env, urgency):
        """Start the pop-ups in a session with env, send a notification and run the main loop."""
        queue = NotificationQueue({}, single_notification_apps=())
        with mock.patch.dict(os.environ, dict(env, DISPLAY=':0', PATH=''), clear=True):
            popups = RoficationPopups.start(queue)
        ntf = notification(urgency)
        ntf.summary = urgency.name
        with queue.lock:
            queue.put(ntf)
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        return popups

    def test_stock_install_shows_no_pop_up(self):
        for urgency in Urgency:
            self.assertIsNone(self.notify({}, urgency))
        # the daemon does not even connect to the display
        self.window_module.init_display.assert_not_called()
        self.window_module.PopupWindow.assert_not_called()
        self.assertEqual(self.shown, [])

    def test_unknown_value_shows_no_pop_up(self):
        self.assertIsNone(self.notify({'rofication_popup_min_urgency': 'loud'}, Urgency.CRITICAL))
        self.assertEqual(self.shown, [])

    def test_explicit_none_shows_no_pop_up(self):
        self.assertIsNone(self.notify({'rofication_popup_min_urgency': 'none'}, Urgency.CRITICAL))
        self.assertEqual(self.shown, [])

    def test_normal_brings_pop_ups_back(self):
        env = {'rofication_popup_min_urgency': 'normal'}
        for urgency in Urgency:
            self.assertIsNotNone(self.notify(env, urgency))
        self.window_module.init_display.assert_called()
        self.assertEqual(self.shown, ['NORMAL', 'CRITICAL'])


class NotificationActionsTest(unittest.TestCase):
    def test_action_pairs_ignore_unpaired_key(self):
        ntf = notification(actions=('one', 'First', 'default', 'Open', 'orphan'))
        self.assertEqual(ntf.action_pairs, (('one', 'First'), ('default', 'Open')))
        self.assertTrue(ntf.has_action('default'))
        self.assertFalse(ntf.has_action('orphan'))

    def test_expire_timeout_is_not_saved(self):
        self.assertNotIn('expire_timeout', notification(expire_timeout=0).asdict())


class DoNotDisturbQueueTest(unittest.TestCase):
    def test_changes_are_announced_once(self):
        queue = NotificationQueue({}, single_notification_apps=())
        changes = []
        queue.do_not_disturb_changed += changes.append
        self.assertTrue(queue.set_do_not_disturb(True))
        self.assertFalse(queue.set_do_not_disturb(True))
        self.assertTrue(queue.set_do_not_disturb(False))
        self.assertEqual(changes, [True, False])

    def test_persisted_next_to_the_queue_file(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'notifications.json')
            queue = NotificationQueue({}, single_notification_apps=())
            queue.set_do_not_disturb(True)
            queue.save_do_not_disturb(filename)
            self.assertFalse(os.path.exists(filename))
            self.assertTrue(NotificationQueue.load(filename).do_not_disturb)

    def test_put_and_invoke_events(self):
        queue = NotificationQueue({}, single_notification_apps=())
        put, invoked = [], []
        queue.notification_put += put.append
        queue.notification_action += lambda ntf, action: invoked.append((ntf.id, action))
        ntf = notification()
        queue.put(ntf)
        queue.invoke(ntf.id, 'later')
        self.assertEqual(put, [ntf])
        self.assertEqual(invoked, [(ntf.id, 'later')])


class DoNotDisturbSocketTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        path = os.path.join(self._dir.name, 'socket')
        self.server = RoficationServer(NotificationQueue({}, ()), path)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.path = path

    def send(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sck:
            sck.connect(self.path)
            sck.sendall(command.encode('utf-8') + b'\n')
            return b''.join(iter(lambda: sck.recv(1024), b''))

    def test_replies(self):
        self.assertEqual(self.send('dnd'), b'off')
        self.assertEqual(self.send('dnd:on'), b'on')
        self.assertEqual(self.send('dnd:maybe'), b'on')
        self.assertEqual(self.send('dnd:toggle'), b'off')

    def test_client(self):
        client = RoficationClient(unix_socket=self.path)
        self.assertTrue(client.do_not_disturb('on'))
        self.assertTrue(client.do_not_disturb())
        self.assertFalse(client.do_not_disturb('toggle'))


if __name__ == '__main__':
    unittest.main()
