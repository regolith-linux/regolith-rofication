import os
import re
from warnings import warn
from typing import Callable, List, Tuple, Pattern, NewType
import subprocess
import threading
import pyinotify
from rofication import Notification, Urgency


class BaseInterceptor:

    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):
        print(f"Intercepted {notification.summary}")
        return False

# Watches a single file
# TODO: The callback provides no information
# it works, but isn't very clear
class Watcher(pyinotify.ProcessEvent):

    def __init__(self, path: str, callback: Callable[[], None]):
        self.path = path
        self.callback = callback

    def process_default(self, event):
        print(f"Event {event}")
        if event.pathname == self.path:
            self.callback()

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



    def __init__(self, config_path='~/.config/regolith/rofications/config'):
        config_path = os.path.expanduser(config_path)

        self.config = {}
        self.matchers = []

        def read():
            print(f"Loading config file")
            self.load_config(config_path)

        folder = os.path.dirname(os.path.abspath(config_path))
        wm1 = pyinotify.WatchManager()
        notifier1 = pyinotify.ThreadedNotifier(wm1, default_proc_fun=Watcher(config_path, read))
        notifier1.start()

        wm1.add_watch(folder, pyinotify.IN_CLOSE_WRITE, rec=True, auto_add=True)

        read()
        print(f"Loaded matchers {self.matchers}")

    def load_config(self, path):
        pass


#https://stackoverflow.com/a/2581943
def popen_and_call(on_exit: Callable[[int], None], popen_args):
    """
    Runs the given args in a subprocess.Popen, and then calls the function
    on_exit when the subprocess completes.
    on_exit is a callable object, and popen_args is a list/tuple of args that
    would give to subprocess.Popen.
    """
    def run_in_thread(on_exit, popen_args):
        print(f"Popen args {popen_args}")
        proc = subprocess.Popen(*popen_args)
        proc.wait()

        on_exit(proc.returncode)
        return
    thread = threading.Thread(target=run_in_thread, args=(on_exit, popen_args))
    thread.start()
    # returns immediately after the thread starts
    return thread



class DefaultInterceptor(ConfiguredInterceptor):

    KEYS = ["all", "summary", "body", "application"]

    Matcher = NewType("Matcher", Tuple[str, Pattern, bool])

    def load_config(self, path):
        if os.path.exists(path):
            pass
        if os.path.isfile(path):
            pass
        with open(path, 'r') as f:
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
                    elif mode == "[list]":
                        self.parse_matcher(line)
                    else:
                        warn(f"Unrecognised config mode {mode}")

    def parse_matcher(self, line):
        if line[0] == '!':
            blacklist = True
            splits = line[:1].split(":", 1)
        else:
            blacklist = False
            splits = line.split(":", 1)
        # TODO: Support whitespace between key and regex. Or some better format
        if len(splits) == 2 and splits[0] in self.KEYS:
            try:
                self.matchers.append((splits[0], re.compile(splits[1]), blacklist))
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

    def get_config_bool(self, key, default=False):
        if key in self.config:
            if self.config[key] in ['true', '1', 't', 'y', 'yes']:
                return True
            elif self.config[key] in ['false', '0', 'f', 'n', 'no']:
                return False
        return default

    def matches(self, notification: Notification, matcher: Matcher):
        k, m, _ = matcher
        if ((k == "all" or k == "summary") and m.match(notification.summary)) or \
                ((k == "all" or k == "body") and m.match(notification.body)) or \
                ((k == "all" or k == "application") and m.match(notification.application)):
            return True

    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):
        if notification.urgency == Urgency.CRITICAL and self.get_config_bool("always_display_critical"):
            self.dispatch_nagbar(notification)
            return

        for m in self.matchers:
            if self.matches(notification, m):
                print(f"Notificaiton matched {m}")
                if m[2]:
                    self.dispatch_nagbar(notification, on_viewed)


    def dispatch_nagbar(self, notification: Notification, on_viewed: Callable[[bool], None]):
        print(f"Displaying nagbar for {notification.summary}")
        subprocess.Popen(("/usr/bin/i3-msg", "fullscreen", "disable"))
        cmd = ("/usr/bin/i3-nagbar", "-m", notification.summary)

        def callback(rc):
            print(f"Nagbar closed with code {rc}")
            on_viewed(rc == 0 and self.get_config_bool("consume_on_dismiss"))

        popen_and_call(callback, (cmd, ))
        #TODO: The nagbar can deal with actions, implement them

