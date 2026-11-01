import argparse
import re
import struct
import subprocess
from typing import Iterable, List, Tuple
from datetime import datetime

from gi.repository import GLib

from ._client import RoficationClient
from ._notification import Urgency, Notification
from ._util import Resource

HTML_TAGS_PATTERN = re.compile(r'<[^>]*?>')

# rofi key bindings and their defaults. Each one can be overridden with the
# Xresource rofication.notify.<option> (for example rofication.notify.kb-custom-1)
# or the environment variable rofication_notify_<option> with '-' replaced by '_'.
# kb-custom-1..4 make rofi exit with codes 10..13, handled in RoficationGui.run().
KEYBINDINGS = (('kb-accept-entry', 'Control+j,Control+m,KP_Enter'),
               ('kb-remove-char-forward', 'Control+d'),
               ('kb-delete-entry', ''),
               ('kb-custom-1', 'Delete'),        # dismiss notification
               ('kb-custom-2', 'Return'),        # mark notification as seen
               ('kb-custom-3', 'Alt+r'),         # refresh
               ('kb-custom-4', 'Shift+Delete'))  # dismiss all notifications of the application

ROFI_COMMAND_START = ('rofi',
                      '-dmenu',
                      '-p', 'Notifications',
                      '-markup')
ROFI_COMMAND_END = ('-markup-rows',
                    '-sep', '\\0',
                    '-format', 'i',
                    '-eh', '2',
                    '-lines', '10')


def keybinding_resource(option: str, default: str) -> Resource:
    return Resource(env_name='rofication_notify_' + option.replace('-', '_'),
                    xres_name='rofication.notify.' + option,
                    default=default)


def make_rofi_command(keybindings: Iterable[Tuple[str, str]]) -> Tuple[str, ...]:
    kb_args = []
    for option, binding in keybindings:
        kb_args += ['-' + option, binding]
    return ROFI_COMMAND_START + tuple(kb_args) + ROFI_COMMAND_END


def fetch_keybindings() -> List[Tuple[str, str]]:
    return [(option, keybinding_resource(option, default).fetch().strip())
            for option, default in KEYBINDINGS]


# rofi command with the default key bindings
ROFI_COMMAND = make_rofi_command(KEYBINDINGS)


def strip_tags(value: str) -> str:
    return GLib.markup_escape_text(HTML_TAGS_PATTERN.sub('', value))


def rofi_entry(notification: Notification, tsformat: str) -> str:
    stripped_summ = strip_tags(notification.summary)
    stripped_app = strip_tags(notification.application)
    stripped_body = strip_tags(' '.join(notification.body.split()))
    formatted_ts = f"{datetime.fromtimestamp(notification.timestamp).strftime(tsformat)} " if tsformat else ""
    return f'<b>{formatted_ts}{stripped_summ}</b> <small>({stripped_app})</small>\n<small>{stripped_body}</small>'

def call_rofi(entries: Iterable[str], additional_args: List[str] = None,
              command: Iterable[str] = ROFI_COMMAND) -> (int, int):
    if additional_args is not None:
        command = list(command) + additional_args

    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    with proc.stdin as stdin:
        for e in entries:
            stdin.write(e.encode('utf-8'))
            stdin.write(struct.pack('B', 0))

    selected = proc.stdout.read().decode('utf-8')
    exit_code = proc.wait()

    if selected:
        return int(selected), exit_code
    else:
        return -1, exit_code


class RoficationOptions:
    def __init__(self) -> None:
        self.delete_seen = False

    def __repr__(self):
        return str(self.__dict__)

    def parse_args(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--delete_seen', action='store_true',
                            help='Auto delete seen notification')
        parser.parse_args(namespace=self)


class RoficationGui():
    def __init__(self, client: RoficationClient = None):
        self._client: RoficationClient = RoficationClient() if client is None else client
        self._tsformat = Resource(env_name='i3xrocks_notify_timestamp_format', xres_name='i3xrocks.notify.timestamp.format', default='').fetch()
        self._rofi_command = make_rofi_command(fetch_keybindings())

    def run(self, options: RoficationOptions = None) -> None:
        selected = 0
        while selected >= 0:
            notifications = []
            entries = []
            urgent = []
            low = []
            args = []

            # reassigns indices of notifications
            for index, notification in enumerate(self._client.list()):
                notifications.append(notification)
                entries.append(rofi_entry(notification, self._tsformat))
                if notification.urgency == Urgency.CRITICAL:
                    urgent.append(str(index))
                if notification.urgency == Urgency.LOW:
                    low.append(str(index))

            if urgent:
                args.append('-u')
                args.append(','.join(urgent))

            if low:
                args.append('-a')
                args.append(','.join(low))

            if selected >= 0:
                args.append('-selected-row')
                args.append(str(selected))

            # Show rofi
            selected, exit_code = call_rofi(entries, args, self._rofi_command)

            if selected >= 0:
                # Dismiss notification
                if exit_code == 10:
                    self._client.delete(notifications[selected].id)
                    # This was the last notification
                    if len(notifications) == 1:
                        break
                # Seen notification
                elif exit_code == 11:
                    self._client.see(notifications[selected].id)
                    if options and options.delete_seen:
                        self._client.delete(notifications[selected].id)
                        # This was the last notification
                        if len(notifications) == 1:
                            break
                # Dismiss all notifications for application
                elif exit_code == 13:
                    self._client.delete_all(notifications[selected].application)
                    # This was the last group of notifications
                    if len(notifications) == 1:
                        break
                elif exit_code != 12:
                    break
