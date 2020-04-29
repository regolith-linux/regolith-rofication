import os
import re
from warnings import warn
import subprocess
from rofication import Notification, Urgency


class BaseInterceptor:

    def intercept(self, notification: Notification):
        print(f"Intercepted {notification.summary}")


class RegExInterceptor(BaseInterceptor):

    KEYS = ["all", "summary", "body", "application"]

    def __init__(self, matchers_path='~/.config/rotifications/matchers'):
        matchers_path = os.path.expanduser(matchers_path)
        self.matchers = []
        with open(matchers_path, 'r') as f:
            for i, line in enumerate(f.readlines()):
                if not self.parse_line(line):
                    warn(f"Could not compile RegEx {line} on {matchers_path} line {i}")
        print(f"Loaded matchers {self.matchers}")
        # TODO: Deal with files that don't exist
        # TODO: Watch the file for updates?

    def parse_line(self, line) -> bool:
        if not line.startswith("#"):
            splits = line.split(":", 1)
            # TODO: Support whitespace between key and regex. Or some beter format
            if len(splits) == 2 and splits[0] in self.KEYS:
                try:
                    self.matchers.append((splits[0], re.compile(splits[1])))
                except re.error:
                    return False
            else:
                return False

        return True


class NagBarInterceptor(RegExInterceptor):


    def intercept(self, notification: Notification):
        if notification.urgency == Urgency.CRITICAL:
            self.dispatch_nagbar(notification)
            return
        for k, m in self.matchers:
            if ((k == "all" or k == "summary") and m.match(notification.summary)) or \
                ((k == "all" or k == "body") and m.match(notification.body)) or \
                ((k == "all" or k == "application") and m.match(notification.application)):
                self.dispatch_nagbar(notification)
                return


    def dispatch_nagbar(self, notification: Notification):
        subprocess.Popen(("/usr/bin/i3-msg", "fullscreen", "disable"))
        cmd = ("/usr/bin/i3-nagbar", "-m", notification.summary)
        subprocess.Popen(cmd)
        print(f"Opened nagbar for {notification.summary}")
        #TODO: The nagbar can deal with actions, implement them