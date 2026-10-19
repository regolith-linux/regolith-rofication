import os
import stat
import tempfile
import unittest
from unittest import mock

from rofication._util import Resource


class ResourceFetchTest(unittest.TestCase):
    """Resource.fetch() with xrescat/trawlcat stubbed on PATH."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.bin = self._dir.name

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, 'w') as fp:
            fp.write('#!/bin/sh\n' + body + '\n')
        os.chmod(path, stat.S_IRWXU)

    def fetch(self, session_type='x11', env_name=None, extra_env=None):
        env = {'PATH': self.bin, 'XDG_SESSION_TYPE': session_type}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env, clear=True):
            return Resource(default='dflt', xres_name='some.key', env_name=env_name).fetch()

    def test_no_getter_returns_default(self):
        self.assertEqual(self.fetch(), 'dflt')
        self.assertEqual(self.fetch(session_type='wayland'), 'dflt')

    def test_xrescat_used(self):
        self.stub('xrescat', 'printf "x:%s:%s" "$1" "$2"')
        self.assertEqual(self.fetch(), 'x:some.key:dflt')

    def test_trawlcat_used_without_xrescat(self):
        self.stub('trawlcat', 'printf "t:%s:%s" "$1" "$2"')
        self.assertEqual(self.fetch(), 't:some.key:dflt')

    def test_x11_prefers_xrescat(self):
        self.stub('xrescat', 'printf x')
        self.stub('trawlcat', 'printf t')
        self.assertEqual(self.fetch(session_type='x11'), 'x')

    def test_wayland_prefers_trawlcat(self):
        self.stub('xrescat', 'printf x')
        self.stub('trawlcat', 'printf t')
        self.assertEqual(self.fetch(session_type='wayland'), 't')

    def test_failing_getter_falls_through(self):
        # trawlcat exits non-zero when trawld is not running
        self.stub('trawlcat', 'echo "Error: Failed to connect to trawld" >&2; exit 1')
        self.assertEqual(self.fetch(session_type='wayland'), 'dflt')
        self.stub('xrescat', 'printf x')
        self.assertEqual(self.fetch(session_type='wayland'), 'x')

    def test_env_var_wins(self):
        self.stub('xrescat', 'printf x')
        self.assertEqual(self.fetch(env_name='SOME_KEY', extra_env={'SOME_KEY': 'e'}), 'e')


if __name__ == '__main__':
    unittest.main()
