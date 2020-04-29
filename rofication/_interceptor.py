import os
import re
from warnings import warn
import subprocess
from rofication import Notification, Urgency


class BaseInterceptor:

    def intercept(self, notification: Notification):
        print(f"Intercepted {notification.summary}")


class RegExInterceptor(BaseInterceptor):



    def __init__(self, matchers_path='~/.config/rotifications/matchers'):
        matchers_path = os.path.expanduser(matchers_path)
        self.matchers = []
        with open(matchers_path, 'r') as f:
            for i, line in enumerate(f.readlines()):
                if not line.startswith("#"):
                    try:
                        self.matchers.append(re.compile(line))
                    except re.error:
                        warn(f"Could not compile RegEx {line} on {matchers_path} line {i}")
        print(f"Loaded matchers {self.matchers}")
        # TODO: Deal with files that don't exist


class NagBarInterceptor(RegExInterceptor):


    def intercept(self, notification: Notification):
        if notification.urgency == Urgency.CRITICAL:
            self.dispatch_nagbar(notification)
            return
        for m in self.matchers:
            if m.match(notification.body) or m.match(notification.summary):
                self.dispatch_nagbar(notification)
                return

    def dispatch_nagbar(self, notification: Notification):
        subprocess.Popen(("/usr/bin/i3-msg", "fullscreen", "disable"))
        cmd = ("/usr/bin/i3-nagbar", "-m", notification.summary)
        subprocess.Popen(cmd)
        print(f"Opened nagbar for {notification.summary}")
        #TODO: The nagbar can deal with actions, implement them