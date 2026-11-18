import json
import os
import socket
import sys
import threading
from socketserver import ThreadingMixIn, UnixStreamServer, BaseRequestHandler
from typing import TextIO

from ._notification import CloseReason, Urgency, Notification
from ._queue import NotificationQueue
from ._static import ROFICATION_UNIX_SOCK


# How long the server waits on a client for its request, and for it to take the reply.
REQUEST_TIMEOUT = 5


class ThreadedUnixStreamServer(ThreadingMixIn, UnixStreamServer):
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever)
        thread.setDaemon(True)
        thread.start()
        return thread


class RoficationRequestHandler(BaseRequestHandler):
    def count(self, fp: TextIO) -> None:
        with self.server.queue.lock:
            crit = 0
            for n in self.server.queue:
                if n.urgency == Urgency.CRITICAL:
                    crit += 1
            fp.write(f'{len(self.server.queue)},{crit}')
            fp.flush()

    def delete(self, nid: int) -> None:
        with self.server.queue.lock:
            self.server.queue.remove(nid, CloseReason.DISMISSED)

    def delete_multi(self, ids: str) -> None:
        with self.server.queue.lock:
            to_remove = [int(i) for i in ids.split(',')]
            self.server.queue.remove_all(to_remove, CloseReason.DISMISSED)

    def delete_all(self, application: str) -> None:
        with self.server.queue.lock:
            to_remove = [n.id for n in self.server.queue
                         if n.application == application]
            self.server.queue.remove_all(to_remove, CloseReason.DISMISSED)

    def list(self, fp: TextIO) -> None:
        with self.server.queue.lock:
            json.dump(list(self.server.queue), fp, default=Notification.asdict)

    def see(self, nid: int) -> None:
        with self.server.queue.lock:
            self.server.queue.see(nid)

    def handle(self) -> None:
        # A client that connects and sends nothing must not hold up the daemon when it stops.
        self.request.settimeout(REQUEST_TIMEOUT)
        with self.server.queue.lock:
            self.server.queue.cleanup()

        with self.request.makefile(mode='rw', encoding='utf-8') as fp:
            # The command ends at the first colon; the rest is its argument, colons included
            # (application names can contain them).
            cmd, _, arg = fp.readline().strip().partition(':')
            try:
                if cmd == 'num':
                    # get number of notifications
                    self.count(fp)
                elif cmd == 'del':
                    # dismiss an item.
                    self.delete(nid=int(arg))
                elif cmd == 'delm':
                    # dismiss list of notifications.
                    self.delete_multi(ids=arg)
                elif cmd == 'dela':
                    # dismiss all items from an application.
                    self.delete_all(application=arg)
                elif cmd == 'list':
                    # getting a listing.
                    self.list(fp)
                elif cmd == 'see':
                    # see an item, set the urgency to normal and activate
                    self.see(nid=int(arg))
            except ValueError:
                # a notification id that isn't a number: nothing to do
                print(f'rofication: ignored the request {cmd!r} with argument {arg!r}', file=sys.stderr)


class RoficationServer(ThreadedUnixStreamServer):
    def __init__(self, queue: NotificationQueue, server_address: str = ROFICATION_UNIX_SOCK) -> None:
        # pre-start cleanup
        if os.path.exists(server_address):
            os.remove(server_address)
        super().__init__(server_address, RoficationRequestHandler)
        self.queue: NotificationQueue = queue
        self._socket_file = _file_identity(server_address)

    def server_bind(self) -> None:
        # Only this user may connect, whatever umask the process was started with.
        umask = os.umask(0o177)
        try:
            super().server_bind()
        finally:
            os.umask(umask)
        os.chmod(self.server_address, 0o600)

    def handle_error(self, request, client_address) -> None:
        # A client that hangs up before it has the reply, or that sends nothing in time, is not an
        # error of the server.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, socket.timeout)):
            return
        super().handle_error(request, client_address)

    def stop(self) -> None:
        """Stop serving, after answering the requests already waiting on the socket."""
        self.shutdown()
        self.socket.setblocking(False)
        while True:
            try:
                request, client_address = self.get_request()
            except OSError:
                break
            self.process_request(request, client_address)

    def __exit__(self, *args) -> None:
        super().__exit__(*args)
        # Remove the UNIX socket after use, unless it is gone or no longer ours: a daemon started
        # since then removes it at start-up and binds its own socket at the same path. Raising here
        # would also stop rofication-daemon before it saves the queue.
        try:
            if _file_identity(self.server_address) == self._socket_file:
                os.remove(self.server_address)
        except FileNotFoundError:
            pass


def _file_identity(path: str) -> tuple:
    st = os.stat(path)
    return st.st_dev, st.st_ino, st.st_ctime_ns
