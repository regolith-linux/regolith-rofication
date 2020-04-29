import os
import re
from warnings import warn
from typing import Callable, List, Tuple, Pattern
import subprocess
import threading
from rofication import Notification, Urgency


class BaseInterceptor:

    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):
        print(f"Intercepted {notification.summary}")
        return False

"""
Loads a configuration file in similar to i3blocks
Composed of three sections:
[config]
key=value 

List of config values
[whitelist]
all:re1
summary:re2
body:re3
application:re4
[blacklist]

Pairs of keys and RegEx matchers.
Keys correspond to a part of the notification. 
"all" matches on all parts.
"""
class ConfiguredInterceptor(BaseInterceptor):

    KEYS = ["all", "summary", "body", "application"]

    def __init__(self, matchers_path='~/.config/regolith/rofications/config'):
        matchers_path = os.path.expanduser(matchers_path)

        self.config = {}
        self.whitelist = []
        self.blacklist = []
        self.matchers = []
        # TODO: Deal with files that don't exist
        # TODO: Watch the file for updates?
        with open(matchers_path, 'r') as f:
            mode = ""
            # TODO: Log errors
            for i, line in enumerate(f.readlines()):
                line = line.rstrip()

                if not line.startswith("#"):
                    if line.startswith("["):
                        mode = line
                        continue
                    if mode == "[config]":
                        self.parse_config_key(line)
                    elif mode == "[whitelist]":
                        self.parse_whitelist(line)
                    elif line == "[blacklist]":
                        self.parse_blacklist(line)
                    else:
                        warn(f"Unrecognised config mode {mode}")

                # if not self.parse_line(line.rstrip('\n')):
                #     warn(f"Could not compile RegEx {line} on {matchers_path} line {i}")
        print(f"Loaded whitelist {self.whitelist}")
        print(f"Loaded blacklist {self.blacklist}")


    def parse_whitelist(self, line) -> bool:
        matcher = self.parse_matcher(line)
        if matcher is not None:
            self.whitelist.append(matcher)
            return True
        return False

    def parse_blacklist(self, line) -> bool:
        matcher = self.parse_matcher(line)
        if matcher is not None:
            self.blacklist.append(matcher)
            return True
        return False

    def parse_matcher(self, line):
        splits = line.split(":", 1)
        # TODO: Support whitespace between key and regex. Or some better format
        if len(splits) == 2 and splits[0] in self.KEYS:
            try:
                return (splits[0], re.compile(splits[1]))
            except re.error:
                pass
        return None


    def parse_config_key(self, line) -> bool:
        splits = line.split("=", 1)
        if len(splits) == 2:
            self.config[splits[0]] = splits[1]
            print(f"Config entry {splits[0]} : {splits[1]}")
            return True
        return False

    def matches_any_in(self, notification: Notification, matchers: List[Tuple[str, Pattern]]) -> bool:
        for k, m in matchers:
            if ((k == "all" or k == "summary") and m.match(notification.summary)) or \
                ((k == "all" or k == "body") and m.match(notification.body)) or \
                ((k == "all" or k == "application") and m.match(notification.application)):
                return True
        return False

    def get_config_bool(self, key, default=False):
        if key in self.config:
            if self.config[key] in ['true', '1', 't', 'y', 'yes']:
                return True
            elif self.config[key] in ['false', '0', 'f', 'n', 'no']:
                return False
        return default

#https://gist.github.com/kirpit/1306188/ab800151c9128db3b763bb9f9ec19fda0df3a843
"""
Run a command in a new thread, with a timeout. Then execute a callback, giving the command return code
"""
class Dispatcher:

    def __init__(self, cmd, callback: Callable[[int], None]):
        self.cmd = cmd
        self.callback = callback
        self.process = None

    def run(self, timeout=0, **kwargs):
        def target(**kwargs):
            self.process = subprocess.Popen(self.cmd, **kwargs)
            self.process.communicate()

        thread = threading.Thread(target=target, kwargs=kwargs)
        thread.start()

        thread.join(timeout)
        if thread.is_alive():
            self.process.terminate()
            thread.join()

        self.callback(self.process.returncode)


class NagBarInterceptor(ConfiguredInterceptor):


    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):
        if notification.urgency == Urgency.CRITICAL and self.get_config_bool("always_display_critical"):
            self.dispatch_nagbar(notification)
            return

        whitelisted = self.matches_any_in(notification, self.whitelist)
        blacklisted = self.matches_any_in(notification, self.blacklist)
        # TODO: Order should be configurable
        if whitelisted and not blacklisted:
            self.dispatch_nagbar(notification, on_viewed)


    def dispatch_nagbar(self, notification: Notification, on_viewed: Callable[[bool], None]):
        print(f"Displaying nagbar for {notification.summary}")
        subprocess.Popen(("/usr/bin/i3-msg", "fullscreen", "disable"))
        cmd = ("/usr/bin/i3-nagbar", "-m", notification.summary)

        def callback(rc):
            print(f"Nagbar closed with code {rc}")
            on_viewed(rc == 0 and self.get_config_bool("consume_on_dismiss"))
        Dispatcher(cmd, callback).run(timeout=30)
        #TODO: The nagbar can deal with actions, implement them

