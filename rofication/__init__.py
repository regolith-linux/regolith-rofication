from ._client import RoficationClient
from ._metadata import ROFICATION_NAME, ROFICATION_VERSION, ROFICATION_URL
from ._notification import Notification, CloseReason, Urgency
from ._queue import NotificationQueue, next_id_filename
from ._runtime import DaemonInstance, StartError
from ._server import RoficationServer
from ._static import __version__, ROFICATION_UNIX_SOCK
from ._util import Event, Resource


def __getattr__(name: str):
    # The D-Bus service and the GUI load GLib. They are imported when first used, so that a daemon
    # settles whether it is the one to run, and tells a running daemon to stop, without that delay.
    if name == 'RoficationDbusService':
        from ._dbus import RoficationDbusService
        return RoficationDbusService
    if name == 'RoficationGui':
        from ._gui import RoficationGui
        return RoficationGui
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
