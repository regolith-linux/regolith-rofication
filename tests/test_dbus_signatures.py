import json
import os
import tempfile
import unittest

import dbus

from rofication._dbus import RoficationDbusObject
from rofication._notification import Urgency
from rofication._queue import NotificationQueue

# org.freedesktop.Notifications as given by the Desktop Notifications Specification 1.2
SPEC_METHODS = {
    'GetCapabilities': ('', 'as'),
    'Notify': ('susssasa{sv}i', 'u'),
    'CloseNotification': ('u', ''),
    'GetServerInformation': ('', 'ssss'),
}
SPEC_SIGNALS = {
    'NotificationClosed': 'uu',
    'ActionInvoked': 'us',
}


class SignatureTest(unittest.TestCase):
    """The D-Bus interface rofication-daemon exports (and introspects) matches the spec,
    so clients that marshal arguments from introspection data, such as `gdbus call`,
    can call it."""

    def test_methods(self):
        for name, (in_sig, out_sig) in SPEC_METHODS.items():
            method = getattr(RoficationDbusObject, name)
            self.assertEqual((method._dbus_in_signature, method._dbus_out_signature), (in_sig, out_sig), name)

    def test_signals(self):
        for name, sig in SPEC_SIGNALS.items():
            self.assertEqual(getattr(RoficationDbusObject, name)._dbus_signature, sig, name)


class VariantHintsTest(unittest.TestCase):
    """Notify() with hints as dbus-python delivers a{sv} values."""

    def notify(self, hints):
        queue = NotificationQueue({}, single_notification_apps=())

        class Service:
            _queue = queue

        nid = RoficationDbusObject.Notify(Service(), 'app', dbus.UInt32(0), '', 'summary', 'body',
                                          dbus.Array([], signature='s'), dbus.Dictionary(hints, signature='sv'),
                                          dbus.Int32(-1))
        return queue, nid

    def test_urgency_byte(self):
        queue, nid = self.notify({'urgency': dbus.Byte(2)})
        self.assertEqual([(n.id, n.urgency) for n in queue], [(nid, Urgency.CRITICAL)])

    def test_mixed_hints_are_saved(self):
        image = dbus.Struct((dbus.Int32(1), dbus.Int32(1), dbus.Int32(4), dbus.Boolean(True), dbus.Int32(8),
                             dbus.Int32(4), dbus.Array([dbus.Byte(1), dbus.Byte(2), dbus.Byte(3), dbus.Byte(4)])),
                            signature='iiibiiay')
        queue, nid = self.notify({'urgency': dbus.Byte(0), 'category': dbus.String('im.received'),
                                  'resident': dbus.Boolean(True), 'x': dbus.Int32(10), 'image-data': image})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'notifications.json')
            queue.save(path)
            with open(path) as fp:
                saved = json.load(fp)
        self.assertEqual(saved[0]['hints']['category'], 'im.received')
        self.assertEqual(saved[0]['hints']['x'], 10)
        self.assertEqual(saved[0]['urgency'], Urgency.LOW)


if __name__ == '__main__':
    unittest.main()
