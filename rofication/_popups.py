import math
import os
from collections import OrderedDict
from typing import Callable, Dict, List, MutableMapping, Optional

from gi.repository import GLib

from ._notification import CloseReason, Notification, Urgency
from ._queue import NotificationQueue
from ._util import Resource

# geometry of the pop-up cards, in pixels: the uppermost card starts POPUP_MARGIN below the top of
# the screen, every card ends POPUP_MARGIN from the right edge, and cards are POPUP_SPACING apart
POPUP_WIDTH = 360
POPUP_MARGIN = 12
POPUP_SPACING = 8

MIN_URGENCIES = {
    'low': Urgency.LOW,
    'normal': Urgency.NORMAL,
    'critical': Urgency.CRITICAL,
    'none': None,
}


def popup_resource(name: str, default: str) -> Resource:
    """A pop-up setting, read from the environment variable named like the resource with '.' replaced by '_'."""
    return Resource(env_name=name.replace('.', '_'), xres_name=name, default=default)


background_resource = popup_resource('rofication.popup.background', '#2E3440')
critical_background_resource = popup_resource('rofication.popup.critical.background', '#BF616A')
foreground_resource = popup_resource('rofication.popup.foreground', '#ECEFF4')
# pop-ups are off unless the user asks for them
min_urgency_resource = popup_resource('rofication.popup.min_urgency', 'none')
max_visible_resource = popup_resource('rofication.popup.max_visible', '3')
timeout_low_resource = popup_resource('rofication.popup.timeout.low', '5000')
timeout_normal_resource = popup_resource('rofication.popup.timeout.normal', '8000')


def parse_whole_number(value: str, minimum: int) -> Optional[int]:
    """The whole number in value if it is at least minimum, otherwise None."""
    value = value.strip()
    try:
        number = int(value)
    except ValueError:
        try:
            number = float(value)
        except ValueError:
            return None
        if not math.isfinite(number) or not number.is_integer():
            return None
        number = int(number)
    return number if number >= minimum else None


def parse_color(value: str) -> Optional[str]:
    """The colour in value as #RRGGBB, or None if GTK cannot use it."""
    import gi
    gi.require_version('Gdk', '3.0')
    from gi.repository import Gdk

    rgba = Gdk.RGBA()
    if not value.strip() or not rgba.parse(value.strip()):
        return None
    # pop-ups are opaque: the alpha channel is ignored
    return '#{:02X}{:02X}{:02X}'.format(*(round(channel * 255) for channel in (rgba.red, rgba.green, rgba.blue)))


def parse_min_urgency(value: str) -> Optional[Urgency]:
    """The lowest urgency that shows a pop-up, None if none does. Unknown values mean none, the default."""
    return MIN_URGENCIES.get(value.strip().lower())


class PopupSettings:
    def __init__(self, background: str = '#2E3440', critical_background: str = '#BF616A',
                 foreground: str = '#ECEFF4', min_urgency: Optional[Urgency] = None,
                 max_visible: int = 3, timeout_low: int = 5000, timeout_normal: int = 8000) -> None:
        self.background: str = background
        self.critical_background: str = critical_background
        self.foreground: str = foreground
        self.min_urgency: Optional[Urgency] = min_urgency
        self.max_visible: int = max_visible
        self.timeout_low: int = timeout_low
        self.timeout_normal: int = timeout_normal

    @classmethod
    def fetch(cls) -> 'PopupSettings':
        """Read the settings from the environment, then the resource getter; unusable values mean the default."""

        def fetch(resource: Resource, parse: Callable[[str], any]) -> any:
            value = parse(resource.fetch())
            return parse(resource.default) if value is None else value

        return cls(background=fetch(background_resource, parse_color),
                   critical_background=fetch(critical_background_resource, parse_color),
                   foreground=fetch(foreground_resource, parse_color),
                   min_urgency=parse_min_urgency(min_urgency_resource.fetch()),
                   max_visible=fetch(max_visible_resource, lambda v: parse_whole_number(v, 1)),
                   timeout_low=fetch(timeout_low_resource, lambda v: parse_whole_number(v, 0)),
                   timeout_normal=fetch(timeout_normal_resource, lambda v: parse_whole_number(v, 0)))

    def shows(self, notification: Notification, do_not_disturb: bool = False) -> bool:
        """Whether the notification gets a pop-up: its urgency reaches the floor and do-not-disturb lets it through."""
        if self.min_urgency is None or notification.urgency < self.min_urgency:
            return False
        return not do_not_disturb or notification.urgency == Urgency.CRITICAL

    def lifetime(self, notification: Notification) -> int:
        """How long the pop-up stays, in milliseconds; 0 keeps it until it is closed."""
        if notification.urgency == Urgency.CRITICAL:
            return 0
        timeout = notification.expire_timeout
        if timeout > 0:
            return timeout
        if timeout == 0:
            return 0
        return self.timeout_low if notification.urgency == Urgency.LOW else self.timeout_normal


class RoficationPopups:
    """Shows new notifications as pop-up cards stacked in the top right corner of the screen.

    Queue events may come from any thread: they are handled in the GLib main loop, in order.
    """

    def __init__(self, queue: NotificationQueue, settings: PopupSettings, make_popup: Callable) -> None:
        self._queue: NotificationQueue = queue
        self._settings: PopupSettings = settings
        # make_popup(notification, settings, on_press, on_action) creates a hidden pop-up window
        self._make_popup: Callable = make_popup
        # visible pop-ups, the most recent first
        self._shown: List = []
        # notifications waiting for a free place, in order of arrival
        self._waiting: MutableMapping[int, Notification] = OrderedDict()
        self._timers: Dict = {}

        queue.notification_put += lambda notification: self._schedule(self._put, notification)
        queue.notification_closed += lambda notification, reason: self._schedule(self._closed, notification.id)
        queue.do_not_disturb_changed += lambda enabled: self._schedule(self._do_not_disturb_changed, enabled)

    @classmethod
    def start(cls, queue: NotificationQueue) -> Optional['RoficationPopups']:
        """Show pop-ups for the queue on the session's display; None when they are off or there is no display."""
        if parse_min_urgency(min_urgency_resource.fetch()) is None:
            # the default: the daemon stays off the display, as without pop-ups
            print('Pop-ups are off: set rofication.popup.min_urgency to low, normal or critical to show them')
            return None
        if not os.getenv('WAYLAND_DISPLAY') and not os.getenv('DISPLAY'):
            print('No display: notifications are not shown as pop-ups')
            return None
        try:
            from ._popup_window import PopupWindow, init_display
        except (ImportError, ValueError) as error:
            print(f'Pop-ups are not available: {error}')
            return None
        settings = PopupSettings.fetch()
        try:
            init_display(settings)
        except RuntimeError as error:
            print(f'Pop-ups are not available: {error}')
            return None
        return cls(queue, settings, PopupWindow)

    @staticmethod
    def _schedule(function: Callable, *args) -> None:
        def run() -> bool:
            function(*args)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run)

    def _find(self, nid: int):
        return next((popup for popup in self._shown if popup.notification.id == nid), None)

    def _put(self, notification: Notification) -> None:
        if self._queue.get(notification.id) is not notification:
            # replaced or removed since: the event that did it takes care of the pop-up
            return
        shows = self._settings.shows(notification, self._queue.do_not_disturb)
        popup = self._find(notification.id)
        if popup is not None:
            if shows:
                # replacement: update the pop-up where it stands and start its lifetime again
                popup.update(notification)
                self._start_timer(popup)
            else:
                self._hide(popup)
        elif notification.id in self._waiting:
            if shows:
                self._waiting[notification.id] = notification
            else:
                del self._waiting[notification.id]
        elif shows:
            self._waiting[notification.id] = notification
        self._refresh()

    def _closed(self, nid: int) -> None:
        popup = self._find(nid)
        if popup is not None:
            self._hide(popup)
        self._waiting.pop(nid, None)
        self._refresh()

    def _do_not_disturb_changed(self, enabled: bool) -> None:
        if not enabled:
            # nothing comes back: notifications that arrived meanwhile stay in the queue only
            return
        for popup in list(self._shown):
            if popup.notification.urgency != Urgency.CRITICAL:
                self._hide(popup)
        for nid, notification in list(self._waiting.items()):
            if notification.urgency != Urgency.CRITICAL:
                del self._waiting[nid]
        self._refresh()

    def _refresh(self) -> None:
        """Show waiting notifications while there is room, and stack the pop-ups."""
        new = []
        while self._waiting and len(self._shown) < self._settings.max_visible:
            _, notification = self._waiting.popitem(last=False)
            popup = self._make_popup(notification, self._settings, self._pressed, self._invoked)
            popup.connect('size-allocate', self._resized)
            self._shown.insert(0, popup)
            self._start_timer(popup)
            new.append(popup)
        self._layout()
        for popup in new:
            popup.show_all()

    def _layout(self) -> None:
        y = POPUP_MARGIN
        for popup in self._shown:
            popup.place(y)
            y += popup.height() + POPUP_SPACING

    def _resized(self, popup, allocation) -> None:
        if popup in self._shown and popup.height() != allocation.height:
            popup.set_height(allocation.height)
            self._layout()

    def _hide(self, popup) -> None:
        self._stop_timer(popup)
        if popup in self._shown:
            self._shown.remove(popup)
        popup.destroy()

    def _start_timer(self, popup) -> None:
        self._stop_timer(popup)
        lifetime = self._settings.lifetime(popup.notification)
        if lifetime > 0:
            self._timers[popup] = GLib.timeout_add(lifetime, self._expired, popup)

    def _stop_timer(self, popup) -> None:
        source = self._timers.pop(popup, None)
        if source is not None:
            GLib.source_remove(source)

    def _expired(self, popup) -> bool:
        # only the pop-up goes: the notification stays in the queue
        self._timers.pop(popup, None)
        if popup in self._shown:
            self._hide(popup)
            self._refresh()
        return GLib.SOURCE_REMOVE

    def _pressed(self, popup, button: int) -> None:
        notification = popup.notification
        if button == 1:
            if notification.has_action('default'):
                self._invoked(popup, 'default')
                return
            with self._queue.lock:
                self._queue.see(notification.id)
        elif button != 3:
            return
        self._hide(popup)
        self._refresh()

    def _invoked(self, popup, action: str) -> None:
        notification = popup.notification
        self._hide(popup)
        with self._queue.lock:
            self._queue.invoke(notification.id, action)
            if not notification.resident:
                self._queue.remove(notification.id, CloseReason.DISMISSED)
        self._refresh()
