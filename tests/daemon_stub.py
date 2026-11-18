"""Runs rofication-daemon with a stand-in for its D-Bus service, so tests can start real daemon
processes without a session bus.

Environment:
  STUB_NOTIFY    comma-separated summaries queued at start; a summary starting with "transient"
                 gets the transient hint
  STUB_LEGACY    used instead of /tmp/rofi_notification_daemon
  STUB_FALLBACK  used instead of /tmp/rofication-<uid> when XDG_RUNTIME_DIR isn't set
"""
import os
import runpy
import signal
import sys
import time

import rofication
from rofication import _runtime, _static
from rofication._notification import Notification

DAEMON = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'rofication-daemon')


class StubDbusService:
    def __init__(self, queue):
        self._stopping = False
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._stop)
        for summary in filter(None, os.environ.get('STUB_NOTIFY', '').split(',')):
            ntf = Notification()
            ntf.application, ntf.summary, ntf.body = 'stub', summary, ''
            if summary.startswith('transient'):
                ntf.hints = {'transient': True}
            with queue.lock:
                queue.put(ntf)

    def _stop(self, signum, frame):
        self._stopping = True

    def run(self):
        while not self._stopping:
            time.sleep(0.02)


if __name__ == '__main__':
    if os.environ.get('STUB_LEGACY'):
        _static.ROFICATION_UNIX_SOCK = os.environ['STUB_LEGACY']
    if os.environ.get('STUB_FALLBACK'):
        fallback = os.environ['STUB_FALLBACK']
        real_runtime_dir = _runtime.runtime_dir
        _runtime.runtime_dir = lambda: (fallback, True) if real_runtime_dir()[1] else real_runtime_dir()
    rofication.RoficationDbusService = StubDbusService
    sys.argv = [DAEMON, *sys.argv[1:]]
    runpy.run_path(DAEMON, run_name='__main__')
