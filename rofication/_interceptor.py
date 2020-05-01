import os
import re
from warnings import warn
from typing import Callable, List, Tuple, Pattern, NewType
import subprocess
import threading
import pyinotify
from rofication import Notification, Urgency

"""
A notification interceptor takes a notification and displays it
"""
class BaseInterceptor:


    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):
        """
        Displays a notification to the user an invoked on_viewed
        :param notification: Notification to display
        :param on_viewed: Callback invoked on view. Boolean argument indicates whether the notification should be
        considered dismissed
        """
        print(f"Intercepted {notification.summary}")
        return False

# TODO: Is there a cleaner way to do this?
class Watcher(pyinotify.ProcessEvent):

    def __init__(self, path: str, callback: Callable[[], None]):
        """
        Watches a single file via pyinotify
        :param path: File to watch
        :param callback: Callback invoked on change
        """
        self.path = path
        self.callback = callback

    def process_default(self, event):
        print(f"Event {event}")
        if event.pathname == self.path:
            self.callback()

"""
Loads a configuration file and then watches it for changes
Each time the file is written to, it invokes load_config
"""
class ConfiguredInterceptor(BaseInterceptor):



    def __init__(self, config_path):

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

    def __init__(self, config_path='~/.config/regolith/rofications/config'):
        ConfiguredInterceptor.__init__(self, config_path)


    def load_config(self, path):
        if not os.path.exists(path):
            warn(f"Path {path} does not exist")
            return
        #TODO: Config file without extension isn't detected as file
        # if os.path.isfile(path):
        #     warn(f"Path {path} is not file")
        #     return
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
        if line[0] == '!':  #blacklist item
            whitelist = False
            splits = line[:1].split(":", 1)
        else:
            whitelist = True
            splits = line.split(":", 1)
        # TODO: Support whitespace between key and regex. Or some better format
        if len(splits) == 2 and splits[0] in self.KEYS:
            try:
                self.matchers.append((splits[0], re.compile(splits[1]), whitelist))
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

    @staticmethod
    def matches(notification: Notification, matcher: Matcher):
        k, m, _ = matcher
        if ((k == "all" or k == "summary") and m.match(notification.summary)) or \
                ((k == "all" or k == "body") and m.match(notification.body)) or \
                ((k == "all" or k == "application") and m.match(notification.application)):
            return True

    def intercept(self, notification: Notification, on_viewed: Callable[[bool], None]):

        #TODO: Should urgency rank above matchers?
        if notification.urgency == Urgency.CRITICAL and self.get_config_bool("always_display_critical"):
            self.dispatch_nagbar(notification, on_viewed)
            return

        for m in self.matchers:
            if self.matches(notification, m):
                print(f"Notification matched {m}")
                if m[2]: # whitelisted
                    self.dispatch_nagbar(notification, on_viewed)
                break


    def dispatch_nagbar(self, notification: Notification, on_viewed: Callable[[bool], None]):
        print(f"Displaying nagbar for {notification.summary}")
        subprocess.Popen(("/usr/bin/i3-msg", "fullscreen", "disable"))
        cmd = ("/usr/bin/i3-nagbar", "-m", notification.summary)

        cmd = "python3", \
              os.path.join(os.path.expanduser("~/"), "Documents/notifbar/bar.py"), \
              "-m {}".format(notification.summary)
        def callback(rc):
            print(f"Nagbar closed with code {rc}")
            on_viewed(rc == 0 and self.get_config_bool("consume_on_dismiss"))

        popen_and_call(callback, (cmd, ))
        #TODO: The nagbar can deal with actions, implement them

