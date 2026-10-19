import os
import stat
import tempfile
import unittest
from unittest import mock

from rofication._gui import RoficationGui, ROFI_COMMAND
from rofication._notification import Notification

# the rofi command line rofication-gui has always used
LEGACY_ROFI_COMMAND = ('rofi',
                       '-dmenu',
                       '-p', 'Notifications',
                       '-markup',
                       '-kb-accept-entry', 'Control+j,Control+m,KP_Enter',
                       '-kb-remove-char-forward', 'Control+d',
                       '-kb-delete-entry', '',
                       '-kb-custom-1', 'Delete',
                       '-kb-custom-2', 'Return',
                       '-kb-custom-3', 'Alt+r',
                       '-kb-custom-4', 'Shift+Delete',
                       '-markup-rows',
                       '-sep', '\\0',
                       '-format', 'i',
                       '-eh', '2',
                       '-lines', '10')

KB_OPTIONS = ('kb-accept-entry', 'kb-remove-char-forward', 'kb-delete-entry',
              'kb-custom-1', 'kb-custom-2', 'kb-custom-3', 'kb-custom-4')


class FakeClient:
    def __init__(self, applications):
        self.notifications = []
        for nid, app in enumerate(applications, 1):
            ntf = Notification()
            ntf.id, ntf.application, ntf.summary, ntf.body, ntf.timestamp = nid, app, 's', 'b', 0
            self.notifications.append(ntf)
        self.deleted = []

    def list(self):
        return list(self.notifications)

    def delete(self, nid):
        self.deleted.append(nid)
        self.notifications = [n for n in self.notifications if n.id != nid]

    def see(self, nid):
        pass

    def delete_all(self, application):
        pass


def option_value(command, option):
    return command[command.index(option) + 1]


class KeybindingsTest(unittest.TestCase):
    """rofication-gui key bindings read through xrescat (stubbed on PATH)."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.bin = self._dir.name

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, 'w') as fp:
            fp.write('#!/bin/sh\n' + body + '\n')
        os.chmod(path, stat.S_IRWXU)

    def stub_resources(self, values):
        # xrescat printing the given values, and the default for any other key
        cases = ''.join(f'  "{key}") printf %s "{value}" ;;\n' for key, value in values.items())
        self.stub('xrescat', 'case "$1" in\n' + cases + '  *) printf %s "$2" ;;\nesac')

    def env(self, extra=None):
        env = {'PATH': self.bin, 'HOME': self.bin}
        env.update(extra or {})
        return mock.patch.dict(os.environ, env, clear=True)

    def gui_command(self, extra_env=None):
        with self.env(extra_env):
            return tuple(RoficationGui(FakeClient([]))._rofi_command)

    def test_module_default_command_unchanged(self):
        self.assertEqual(tuple(ROFI_COMMAND), LEGACY_ROFI_COMMAND)

    def test_defaults_when_resources_unset(self):
        self.stub_resources({})
        self.assertEqual(self.gui_command(), LEGACY_ROFI_COMMAND)

    def test_resource_names_and_defaults(self):
        self.stub('xrescat', 'printf "%s=%s\\n" "$1" "$2" >> "${0%/*}/lookups"; printf %s "$2"')
        self.gui_command()
        with open(os.path.join(self.bin, 'lookups')) as fp:
            lookups = fp.read().splitlines()
        expected = [f'i3xrocks.notify.{option}={option_value(LEGACY_ROFI_COMMAND, "-" + option)}'
                    for option in KB_OPTIONS]
        for line in expected:
            self.assertIn(line, lookups)

    def test_resource_overrides_one_binding(self):
        self.stub_resources({'i3xrocks.notify.kb-custom-1': 'Control+x'})
        command = self.gui_command()
        self.assertEqual(option_value(command, '-kb-custom-1'), 'Control+x')
        expected = list(LEGACY_ROFI_COMMAND)
        expected[expected.index('-kb-custom-1') + 1] = 'Control+x'
        self.assertEqual(command, tuple(expected))

    def test_resource_overrides_every_binding(self):
        values = {f'i3xrocks.notify.{option}': f'Super+{n}' for n, option in enumerate(KB_OPTIONS)}
        self.stub_resources(values)
        command = self.gui_command()
        for n, option in enumerate(KB_OPTIONS):
            self.assertEqual(option_value(command, '-' + option), f'Super+{n}')
        self.assertEqual(len(command), len(LEGACY_ROFI_COMMAND))

    def test_environment_variable_wins(self):
        self.stub_resources({'i3xrocks.notify.kb-custom-2': 'Control+s'})
        command = self.gui_command({'i3xrocks_notify_kb_custom_2': 'Control+Return'})
        self.assertEqual(option_value(command, '-kb-custom-2'), 'Control+Return')

    def test_run_passes_bindings_to_rofi(self):
        # rofi stub: record argv, select row 0 with kb-custom-1 (exit 10) once, then cancel
        self.stub_resources({'i3xrocks.notify.kb-custom-1': 'Control+x'})
        self.stub('rofi', 'while IFS= read -r _; do :; done\n'
                          'log="${0%/*}/rofi.log"\n'
                          'for a in "$@"; do printf "%s\\n" "$a" >> "$log"; done; echo -- >> "$log"\n'
                          'if [ ! -e "${0%/*}/once" ]; then : > "${0%/*}/once"; printf 0; exit 10; fi\n'
                          'exit 1')
        client = FakeClient(['app', 'app'])
        with self.env():
            RoficationGui(client).run()
        self.assertEqual(client.deleted, [1])
        with open(os.path.join(self.bin, 'rofi.log')) as fp:
            calls = [c.strip('\n').split('\n') for c in fp.read().split('--\n') if c]
        self.assertEqual(len(calls), 2)
        for argv in calls:
            self.assertEqual(option_value(argv, '-kb-custom-1'), 'Control+x')
            self.assertEqual(option_value(argv, '-kb-custom-4'), 'Shift+Delete')


if __name__ == '__main__':
    unittest.main()
