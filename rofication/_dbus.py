import signal
import time
from typing import Tuple, Sequence, Mapping

import dbus
from dbus import service, SessionBus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from gi.repository.GLib import MainLoop

from ._metadata import ROFICATION_VERSION, ROFICATION_NAME, ROFICATION_URL
from ._notification import CloseReason, Notification, Urgency
from ._queue import NotificationQueue
from datetime import datetime

NOTIFICATIONS_DBUS_INTERFACE = 'org.freedesktop.Notifications'
NOTIFICATIONS_DBUS_OBJECT_PATH = '/org/freedesktop/Notifications'

ROFICATION_DBUS_NAME = 'org.regolith.Rofication'
ROFICATION_DBUS_INTERFACE = 'org.regolith.Rofication1'
ROFICATION_DBUS_OBJECT_PATH = '/org/regolith/Rofication'
# introspection data of the interface: dbus-python only introspects methods and signals
ROFICATION_DBUS_INTERFACE_XML = f'''  <interface name="{ROFICATION_DBUS_INTERFACE}">
    <property name="DoNotDisturb" type="b" access="readwrite">
      <annotation name="org.freedesktop.DBus.Property.EmitsChangedSignal" value="true"/>
    </property>
  </interface>
'''


def urgency_from_hint(value) -> Urgency:
    """The urgency of a Notify() urgency hint. The specification defines 0, 1 and 2; any other
    value, or one that isn't a number, is taken as normal, so the notification isn't lost."""
    try:
        return Urgency(int(value))
    except (TypeError, ValueError):
        return Urgency.NORMAL


class RoficationDbusObject(service.Object):
    def __init__(self, queue: NotificationQueue) -> None:
        super().__init__(
            object_path=NOTIFICATIONS_DBUS_OBJECT_PATH,
            bus_name=service.BusName(
                name=NOTIFICATIONS_DBUS_INTERFACE,
                bus=SessionBus(mainloop=DBusGMainLoop())
            )
        )
        self._queue: NotificationQueue = queue

        def notification_seen(notification):
            if 'default' in notification.actions:
                self.ActionInvoked(notification.id, 'default')

        self._queue.notification_seen += notification_seen

        def notification_closed(notification, reason):
            self.NotificationClosed(notification.id, reason)

        self._queue.notification_closed += notification_closed

        def notification_action(notification, action_key):
            self.ActionInvoked(notification.id, action_key)

        self._queue.notification_action += notification_action

    @service.signal(NOTIFICATIONS_DBUS_INTERFACE, signature='us')
    def ActionInvoked(self, id_in, action_key_in):
        pass

    @service.method(NOTIFICATIONS_DBUS_INTERFACE, in_signature='u', out_signature='')
    def CloseNotification(self, id: int) -> None:
        with self._queue.lock:
            self._queue.remove(id, CloseReason.CLOSED)

    @service.method(NOTIFICATIONS_DBUS_INTERFACE, in_signature='', out_signature='as')
    def GetCapabilities(self) -> Sequence[str]:
        # the queue keeps every notification until the user dismisses it or the sender closes it
        return 'actions', 'body', 'persistence'

    @service.method(NOTIFICATIONS_DBUS_INTERFACE, in_signature='', out_signature='ssss')
    def GetServerInformation(self) -> Tuple[str, str, str, str]:
        return ROFICATION_NAME, ROFICATION_URL, ROFICATION_VERSION, '1.2'

    @service.signal(NOTIFICATIONS_DBUS_INTERFACE, signature='uu')
    def NotificationClosed(self, id_in, reason_in):
        pass

    @service.method(NOTIFICATIONS_DBUS_INTERFACE, in_signature='susssasa{sv}i', out_signature='u')
    def Notify(self, app_name: str, replaces_id: int, app_icon: str, summary: str,
               body: str, actions: Sequence[str], hints: Mapping[str, any], expire_timeout: int) -> int:
        notification = Notification()
        notification.id = replaces_id
        notification.application = app_name
        notification.icon = app_icon
        notification.summary = summary
        notification.body = body
        notification.hints = hints
        notification.timestamp = datetime.now().timestamp()
        notification.actions = tuple(actions)
        notification.expire_timeout = int(expire_timeout)
        if int(expire_timeout) > 0:
            notification.deadline = time.time() + expire_timeout / 1000.0
        if 'urgency' in hints:
            notification.urgency = urgency_from_hint(hints['urgency'])
        with self._queue.lock:
            self._queue.put(notification)
        return notification.id


class UnknownInterface(dbus.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.UnknownInterface'


class UnknownProperty(dbus.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.UnknownProperty'


class InvalidArgs(dbus.DBusException):
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'


class RoficationControlObject(service.Object):
    """org.regolith.Rofication1: the daemon's own settings, as D-Bus properties."""

    def __init__(self, queue: NotificationQueue) -> None:
        super().__init__(
            object_path=ROFICATION_DBUS_OBJECT_PATH,
            bus_name=service.BusName(
                name=ROFICATION_DBUS_NAME,
                bus=SessionBus(mainloop=DBusGMainLoop())
            )
        )
        self._queue: NotificationQueue = queue

        def do_not_disturb_changed(enabled):
            self.PropertiesChanged(ROFICATION_DBUS_INTERFACE,
                                   dbus.Dictionary({'DoNotDisturb': dbus.Boolean(enabled)}, signature='sv'),
                                   dbus.Array([], signature='s'))

        self._queue.do_not_disturb_changed += do_not_disturb_changed

    def _properties(self) -> Mapping[str, any]:
        return dbus.Dictionary({'DoNotDisturb': dbus.Boolean(self._queue.do_not_disturb)}, signature='sv')

    @staticmethod
    def _check_interface(interface: str) -> None:
        if interface != ROFICATION_DBUS_INTERFACE:
            raise UnknownInterface(f'No such interface: {interface}')

    @service.method(dbus.PROPERTIES_IFACE, in_signature='ss', out_signature='v')
    def Get(self, interface: str, name: str) -> any:
        self._check_interface(interface)
        properties = self._properties()
        if name not in properties:
            raise UnknownProperty(f'No such property: {name}')
        return properties[name]

    @service.method(dbus.PROPERTIES_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface: str) -> Mapping[str, any]:
        self._check_interface(interface)
        return self._properties()

    @service.method(dbus.PROPERTIES_IFACE, in_signature='ssv', out_signature='')
    def Set(self, interface: str, name: str, value: any) -> None:
        self._check_interface(interface)
        if name != 'DoNotDisturb':
            raise UnknownProperty(f'No such property: {name}')
        if not isinstance(value, dbus.Boolean):
            raise InvalidArgs(f'{name} takes a boolean')
        with self._queue.lock:
            self._queue.set_do_not_disturb(bool(value))

    @service.signal(dbus.PROPERTIES_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed_properties, invalidated_properties):
        pass

    @service.method(dbus.INTROSPECTABLE_IFACE, in_signature='', out_signature='s',
                    path_keyword='object_path', connection_keyword='connection')
    def Introspect(self, object_path, connection):
        xml = super().Introspect(object_path, connection)
        if object_path != ROFICATION_DBUS_OBJECT_PATH:
            return xml
        index = xml.rfind('</node>')
        return xml[:index] + ROFICATION_DBUS_INTERFACE_XML + xml[index:]


class RoficationDbusService:
    def __init__(self, queue: NotificationQueue) -> None:
        # preserve D-Bus object references
        self._object = RoficationDbusObject(queue)
        self._control_object = RoficationControlObject(queue)
        # create GLib mainloop, this is needed to make D-Bus work and takes care of catching signals.
        self._loop = MainLoop()
        # Stop the loop on the signals a session or the user sends to end the daemon, so the caller
        # gets to save the queue. Without this, SIGTERM kills the process before the queue is saved
        # and SIGINT is ignored while the loop runs.
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._quit_on_signal, signum)

    def _quit_on_signal(self, signum: int) -> bool:
        print(f'Received signal {signum}, stopping')
        self._loop.quit()
        return GLib.SOURCE_REMOVE

    def run(self) -> None:
        self._loop.run()
