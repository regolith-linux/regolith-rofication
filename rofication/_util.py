import os
from collections.abc import MutableSequence, Callable
from subprocess import CalledProcessError, check_output
from typing import Optional

class Event:
    def __init__(self) -> None:
        self._observers: MutableSequence[Callable] = []

    def __iadd__(self, observer: Callable) -> 'Event':
        self._observers.append(observer)
        return self

    def notify(self, *args, **kwargs) -> None:
        for observer in self._observers:
            observer(*args, **kwargs)


class Resource:
    def __init__(self, default: str, xres_name: str, env_name: Optional[str] = None):
        self.default: str = default
        self.xres_name: str = xres_name
        self.env_name: Optional[str] = env_name

    def fetch(self) -> str:
        env_val = None
        if self.env_name:
            env_val = os.getenv(self.env_name)

        # avoid calling a resource getter if the environment variable is set
        if env_val is not None:
            return env_val

        # Regolith reads resources with xrescat (X11) or trawlcat (Wayland);
        # either may be missing, so try both and fall back to the default.
        getters = ('xrescat', 'trawlcat')
        if os.getenv('XDG_SESSION_TYPE') == 'wayland':
            getters = getters[::-1]
        for getter in getters:
            try:
                return check_output((getter, self.xres_name, self.default), universal_newlines=True)
            except (OSError, CalledProcessError):
                continue
        return self.default
