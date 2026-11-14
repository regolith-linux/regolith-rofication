import os

import gi

gi.require_version('Gdk', '3.0')
gi.require_version('Gtk', '3.0')
GtkLayerShell = None
if os.getenv('WAYLAND_DISPLAY'):
    # load gtk-layer-shell before GTK connects to the compositor
    try:
        gi.require_version('GtkLayerShell', '0.1')
        from gi.repository import GtkLayerShell
    except (ImportError, ValueError):
        GtkLayerShell = None
from gi.repository import Gdk, Gtk, Pango

from ._notification import Notification, Urgency
from ._popups import POPUP_MARGIN, POPUP_WIDTH, PopupSettings

# inside every edge of a card, a band of this many pixels shows nothing but the background
POPUP_PADDING = 12

POPUP_CSS = '''
window.rofication-popup {{
    background-color: {background};
    background-image: none;
    border: none;
    border-radius: 0;
    box-shadow: none;
}}
window.rofication-popup.critical {{
    background-color: {critical_background};
}}
window.rofication-popup label {{
    color: {foreground};
    text-shadow: none;
}}
window.rofication-popup label.summary {{
    font-weight: bold;
}}
window.rofication-popup label.application {{
    font-size: smaller;
}}
window.rofication-popup button {{
    color: {foreground};
    background-color: transparent;
    background-image: none;
    border: 1px solid {foreground};
    border-radius: 3px;
    box-shadow: none;
    text-shadow: none;
    padding: 2px 8px;
}}
window.rofication-popup button:hover {{
    background-color: alpha({foreground}, 0.2);
}}
'''

_layer_shell = False


def init_display(settings: PopupSettings) -> None:
    """Prepare the default display for pop-ups; RuntimeError if they cannot be shown there."""
    global _layer_shell
    display = Gdk.Display.get_default()
    if display is None:
        raise RuntimeError('cannot open the display')
    # gtk-layer-shell is only supported on a Wayland compositor that offers the protocol
    _layer_shell = GtkLayerShell is not None and GtkLayerShell.is_supported()
    if not _layer_shell and display.__gtype__.name == 'GdkWaylandDisplay':
        # without it a Wayland compositor would place, focus or refuse the cards like any window
        raise RuntimeError('the compositor does not support gtk-layer-shell')
    provider = Gtk.CssProvider()
    provider.load_from_data(POPUP_CSS.format(background=settings.background,
                                             critical_background=settings.critical_background,
                                             foreground=settings.foreground).encode('utf-8'))
    Gtk.StyleContext.add_provider_for_screen(display.get_default_screen(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _monitor_geometry() -> Gdk.Rectangle:
    display = Gdk.Display.get_default()
    monitor = display.get_primary_monitor() or display.get_monitor(0)
    return monitor.get_geometry()


class PopupWindow(Gtk.Window):
    """One notification card. It never takes the keyboard and the window manager leaves it alone:
    a layer-shell overlay surface on Wayland, an override-redirect window on X11."""

    def __init__(self, notification: Notification, settings: PopupSettings, on_press, on_action) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL if _layer_shell else Gtk.WindowType.POPUP)
        self.notification: Notification = notification
        self._on_press = on_press
        self._on_action = on_action
        self._height: int = 0

        self.set_title('rofication')
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_size_request(POPUP_WIDTH, -1)
        self.get_style_context().add_class('rofication-popup')
        # the mouse button pressed on the card itself, acted on when it is released
        self._pressed_button = None
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK)
        self.connect('button-press-event', self._button_pressed)
        self.connect('button-release-event', self._button_released)

        if _layer_shell:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_namespace(self, 'rofication')
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.RIGHT, POPUP_MARGIN)

        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._box.set_border_width(POPUP_PADDING)
        self.add(self._box)
        self.update(notification)

    @staticmethod
    def _label(text: str, css_class: str) -> Gtk.Label:
        # plain text: markup in the notification is shown as written
        label = Gtk.Label(label=text)
        label.get_style_context().add_class(css_class)
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        # wrap at the card's width instead of widening the card
        label.set_max_width_chars(1)
        label.set_hexpand(True)
        return label

    def update(self, notification: Notification) -> None:
        """Show the notification's heading, body, application and actions; empty texts are left out."""
        self.notification = notification
        for child in self._box.get_children():
            self._box.remove(child)
            child.destroy()

        style = self.get_style_context()
        if notification.urgency == Urgency.CRITICAL:
            style.add_class('critical')
        else:
            style.remove_class('critical')

        for text, css_class in ((notification.summary, 'summary'), (notification.body, 'body'),
                                (notification.application, 'application')):
            if text:
                self._box.pack_start(self._label(text, css_class), False, False, 0)

        actions = [(key, label) for key, label in notification.action_pairs if key != 'default']
        if actions:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
            row.set_margin_top(4)
            for key, label in actions:
                button = Gtk.Button(label=label)
                button.set_can_focus(False)
                button.set_focus_on_click(False)
                button_label = button.get_child()
                if isinstance(button_label, Gtk.Label):
                    button_label.set_ellipsize(Pango.EllipsizeMode.END)
                    button_label.set_max_width_chars(1)
                button.connect('clicked', lambda _, action=key: self._on_action(self, action))
                row.pack_start(button, True, True, 0)
            self._box.pack_start(row, False, False, 0)

        self._box.show_all()
        _, self._height = self.get_preferred_height_for_width(POPUP_WIDTH)
        self.resize(POPUP_WIDTH, self._height)

    def height(self) -> int:
        return self._height

    def set_height(self, height: int) -> None:
        self._height = height

    def place(self, y: int) -> None:
        """Put the card y pixels below the top edge of the screen, POPUP_MARGIN from the right edge."""
        if _layer_shell:
            if GtkLayerShell.get_margin(self, GtkLayerShell.Edge.TOP) != y:
                GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, y)
                if self.get_mapped():
                    # The new margin only applies when the surface commits, and a surface that a newer
                    # card covers gets no frame callbacks, so it would never commit: map it again.
                    self.hide()
                    self.show()
        else:
            geometry = _monitor_geometry()
            self.move(geometry.x + geometry.width - POPUP_MARGIN - POPUP_WIDTH, geometry.y + y)

    def _button_pressed(self, widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        # presses on the action buttons are theirs
        if event.type != Gdk.EventType.BUTTON_PRESS or Gtk.get_event_widget(event) is not self:
            return False
        if event.button in (Gdk.BUTTON_PRIMARY, Gdk.BUTTON_SECONDARY):
            self._pressed_button = event.button
            return True
        return False

    def _button_released(self, widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        # Act on the release, like a button does: a card closed while the button is still down
        # never gets the release, and on Wayland the next cards then miss their clicks.
        button, self._pressed_button = self._pressed_button, None
        if button is None or event.button != button:
            return False
        allocation = self.get_allocation()
        if 0 <= event.x < allocation.width and 0 <= event.y < allocation.height:
            # released over the card: a release elsewhere cancels the click
            self._on_press(self, button)
        return True
