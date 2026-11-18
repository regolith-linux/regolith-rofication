import json
import socket
from typing import Optional, TextIO, Sequence

from ._notification import Notification
from ._runtime import client_socket_paths, connect_own
from ._static import nullio


class RoficationClient:
    def __init__(self, out: TextIO = nullio, unix_socket: Optional[str] = None):
        """Without unix_socket, the client tries the socket in the runtime directory of the user, then the
        old shared path. A socket served by a process of another user counts as no daemon at all."""
        self._out: TextIO = out
        self._unix_socket: Optional[str] = unix_socket

    def _client_socket(self) -> socket.socket:
        if self._unix_socket is not None:
            return connect_own(self._unix_socket)
        error = None
        for path in client_socket_paths():
            try:
                return connect_own(path)
            except OSError as e:
                error = e
        raise error

    def _send(self, command: str, arg: any) -> None:
        with self._client_socket() as sck:
            sck.send(bytes(f'{command}:{arg}\n', encoding='utf-8'))

    def count(self) -> (int, int):
        with self._client_socket() as sck:
            sck.send(b'num\n')
            data = sck.recv(32).decode('utf-8')
            num, crit = data.split(',')
            return int(num), int(crit)

    def delete(self, nid: int) -> None:
        self._send('del', nid)

    def delete_multi(self, ids: str) -> None:
            self._send('delm', ids)

    def delete_all(self, application: str) -> None:
        self._send('dela', application)

    def list(self) -> Sequence[Notification]:
        with self._client_socket() as sck:
            sck.send(b'list\n')
            with sck.makefile(mode='r', encoding='utf-8') as fp:
                return json.load(fp, object_hook=Notification.make)

    def see(self, nid: int) -> None:
        self._send('see', nid)
