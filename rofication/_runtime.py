import errno
import fcntl
import json
import os
import secrets
import signal
import socket
import stat
import struct
import threading
import time
from typing import List, Mapping, Optional, Sequence, Set, Tuple, Union

from . import _static
from ._notification import Notification
from ._queue import NotificationQueue

# The socket keeps the name it had in /tmp; it now lives in the runtime directory of the user.
SOCKET_NAME = os.path.basename(_static.ROFICATION_UNIX_SOCK)

# How long a replacing daemon waits for the running one to stop.
REPLACE_TIMEOUT = 10

# How long a stopping daemon tries to hand what it holds over to the daemon that replaces it.
HANDOVER_TIMEOUT = 5

# struct flock on Linux: l_type, l_whence, l_start, l_len, l_pid
_FLOCK = 'hhqqi'


class StartError(Exception):
    """The daemon can't start; the message says why, and names the path involved."""


def runtime_dir() -> Tuple[str, bool]:
    """The directory the daemon serves from, and whether it is the fallback directory under /tmp."""
    value = os.environ.get('XDG_RUNTIME_DIR')
    if value:
        return value, False
    return f'/tmp/rofication-{os.getuid()}', True


def _own_directory(st: os.stat_result) -> bool:
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def client_socket_paths() -> List[str]:
    """The sockets a client tries, in order: the one in the runtime directory, then the old shared path.
    Nothing is created or changed; a runtime directory that can't be used is skipped."""
    path, fallback = runtime_dir()
    paths = []
    if fallback:
        try:
            if _own_directory(os.lstat(path)):
                paths.append(os.path.join(path, SOCKET_NAME))
        except OSError:
            pass
    else:
        paths.append(os.path.join(path, SOCKET_NAME))
    legacy = _static.ROFICATION_UNIX_SOCK
    try:
        # Another account's link could lead to some other socket of this user, which would pass the
        # check on the answering process.
        st = os.lstat(legacy)
        if not stat.S_ISLNK(st.st_mode) or st.st_uid == os.getuid():
            paths.append(legacy)
    except OSError:
        pass
    return paths


def peer_credentials(sck: socket.socket) -> Tuple[int, int, int]:
    """The pid, uid and gid of the process at the other end of a connected UNIX socket."""
    creds = sck.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', creds)


def connect_own(path: str, timeout: Optional[float] = None) -> socket.socket:
    """Connect to the UNIX socket at path, but only when the process answering runs as this user.
    Nothing is sent before that is established."""
    sck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sck.settimeout(timeout)
        sck.connect(path)
        _, uid, _ = peer_credentials(sck)
        if uid != os.getuid():
            raise ConnectionRefusedError(errno.ECONNREFUSED, 'answered by another user', path)
    except BaseException:
        sck.close()
        raise
    return sck


# Socket diagnostics (linux/netlink.h, linux/sock_diag.h, linux/unix_diag.h)
_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST = 0x1
_NLM_F_DUMP = 0x300
_NLMSG_ERROR = 2
_NLMSG_DONE = 3
_TCP_LISTEN = 10
_UDIAG_SHOW_VFS = 0x2
_UNIX_DIAG_VFS = 1
_SO_ACCEPTCON = 0x10000


def _listening_socket_files() -> Set[Tuple[int, int]]:
    """The file system device and inode of every listening UNIX socket bound to a path, as the
    kernel's socket diagnostics report them."""
    with socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, _NETLINK_SOCK_DIAG) as nl:
        nl.settimeout(1)
        request = struct.pack('=BBHIIIII', socket.AF_UNIX, 0, 0, 1 << _TCP_LISTEN, 0, _UDIAG_SHOW_VFS, 0, 0)
        nl.sendto(struct.pack('=IHHII', 16 + len(request), _SOCK_DIAG_BY_FAMILY,
                              _NLM_F_REQUEST | _NLM_F_DUMP, 1, 0) + request, (0, 0))
        found = set()
        while True:
            data = nl.recv(65536)
            offset = 0
            while offset + 16 <= len(data):
                length, kind = struct.unpack_from('=IH', data, offset)
                if length < 16 or offset + length > len(data):
                    raise OSError(errno.EPROTO, 'malformed socket diagnostics reply')
                if kind == _NLMSG_DONE:
                    return found
                if kind == _NLMSG_ERROR:
                    code = -struct.unpack_from('=i', data, offset + 16)[0]
                    raise OSError(code, os.strerror(code))
                if kind == _SOCK_DIAG_BY_FAMILY:
                    # unix_diag_msg is 16 bytes; its attributes follow
                    attribute, end = offset + 32, offset + length
                    while attribute + 4 <= end:
                        size, attribute_kind = struct.unpack_from('=HH', data, attribute)
                        if size < 4:
                            break
                        if attribute_kind == _UNIX_DIAG_VFS and size >= 12:
                            inode, device = struct.unpack_from('=II', data, attribute + 4)
                            found.add((device, inode))
                        attribute += (size + 3) & ~3
                offset += (length + 3) & ~3


def _listed_as_listening(path: str) -> bool:
    """Whether the kernel's table of UNIX sockets has a listening socket bound to path. The table
    shows the name given when binding, so a relative or unnormalised name with the same last
    component counts as well."""
    with open('/proc/net/unix') as sockets:
        next(sockets)
        for row in sockets:
            fields = row.rstrip('\n').split(None, 7)
            if len(fields) < 8 or not int(fields[3], 16) & _SO_ACCEPTCON or fields[7].startswith('@'):
                continue
            name = fields[7]
            if name.startswith('/'):
                if os.path.normpath(name) == path:
                    return True
            elif os.path.basename(name) == os.path.basename(path):
                return True
    return False


def _has_listener(path: str) -> bool:
    """Whether some process listens on the socket file at path, decided without connecting to it
    or changing it. Either kernel view finding a listener counts; when neither can be read, the
    socket counts as in use."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISSOCK(st.st_mode):
        return True
    seen = False
    try:
        # the kernel reports its own device number and a 32-bit inode number
        device = os.major(st.st_dev) << 20 | os.minor(st.st_dev)
        if (device, st.st_ino & 0xffffffff) in _listening_socket_files():
            return True
        seen = True
    except OSError:
        pass
    try:
        if _listed_as_listening(path):
            return True
        seen = True
    except (OSError, ValueError, StopIteration):
        pass
    return not seen


def _is_listening(path: str) -> bool:
    """Whether something accepts connections on the socket at path. Only a refused connection, or
    a denied one for a socket the kernel lists no listener for, counts as nothing listening."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sck:
        sck.settimeout(1)
        try:
            sck.connect(path)
        except ConnectionRefusedError:
            return False
        except PermissionError:
            # The socket's mode can refuse this user even when its listener is gone.
            return _has_listener(path)
        except OSError:
            return True
    return True


class _InstanceLock:
    """A POSIX lock on a file next to something a daemon uses, held for as long as the daemon runs.
    The file is opened once and kept open: closing any descriptor of it would drop the lock."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        self._fd: Optional[int] = None

    def _open(self, create: bool) -> bool:
        if self._fd is None:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                self._fd = os.open(self.path, flags | os.O_CREAT if create else flags, 0o600)
            except (FileNotFoundError, NotADirectoryError):
                if create:
                    raise
                return False
            st = os.fstat(self._fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                # e.g. planted by another account in a fallback directory that was not yet 0700
                os.close(self._fd)
                self._fd = None
                raise StartError(f'{self.path} is not a file that belongs to this user')
        return True

    def holder(self) -> Optional[int]:
        """The process that holds the lock, or None. This neither takes the lock nor creates the file."""
        try:
            if not self._open(create=False):
                return None
            query = struct.pack(_FLOCK, fcntl.F_WRLCK, os.SEEK_SET, 0, 0, 0)
            l_type, _, _, _, pid = struct.unpack(_FLOCK, fcntl.fcntl(self._fd, fcntl.F_GETLK, query))
        except OSError as error:
            raise StartError(f'cannot check {self.path}: {error.strerror}')
        return None if l_type == fcntl.F_UNLCK else pid

    def acquire(self) -> bool:
        try:
            self._open(create=True)
            fcntl.lockf(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(self._fd, 0o600)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN) and self._fd is not None:
                return False
            raise StartError(f'cannot lock {self.path}: {error.strerror}')
        return True

    def read_info(self) -> Mapping[str, any]:
        try:
            info = json.loads(os.pread(self._fd, 65536, 0).decode('utf-8'))
            return info if isinstance(info, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_info(self, info: Mapping[str, any]) -> None:
        data = json.dumps(info).encode('utf-8')
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, data, 0)


def _describe(pids) -> str:
    """Names the processes holding a daemon's locks, for messages."""
    known = [str(pid) for pid in pids if pid and pid > 0]
    return f'pid {", ".join(known)}' if known else 'in a process not visible from here'


def _handover_address(token: str) -> str:
    """The abstract UNIX socket a replacing daemon listens on, to take over what the daemon that wrote
    token in its lock files holds. Nothing is created for it in the file system. Both ends check who
    is at the other end, so a queue is only ever handed from that daemon to a daemon of its user."""
    return f'\0rofication-handover-{token}'


class _HandoverReceiver:
    """Listens, from before a running daemon is told to stop, for that daemon to hand over its final
    queue: every notification it still holds, transient ones included, once it takes no more in."""

    def __init__(self, token: str, pid: int) -> None:
        self._pid: int = pid
        self._data: Optional[bytes] = None
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listener: socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(_handover_address(token))
            self._listener.listen(4)
            self._listener.settimeout(0.05)
        except OSError:
            self._listener.close()
            return
        self._thread = threading.Thread(target=self._receive, daemon=True)
        self._thread.start()

    def _receive(self) -> None:
        while True:
            # Read the flag before waiting: the daemon connects before it stops, so once it has
            # stopped, a queue it handed over is already waiting when this accept() starts.
            stopped = self._stopped.is_set()
            try:
                sck, _ = self._listener.accept()
            except socket.timeout:
                if stopped:
                    return
                continue
            except OSError:
                return
            with sck:
                try:
                    pid, uid, _ = peer_credentials(sck)
                    if pid != self._pid or uid != os.getuid():
                        continue
                    sck.settimeout(HANDOVER_TIMEOUT)
                    chunks = list(iter(lambda: sck.recv(65536), b''))
                except OSError:
                    return
            self._data = b''.join(chunks)
            return

    def close(self) -> None:
        self._stopped.set()
        self._listener.close()

    def queue(self) -> Optional[Tuple[List[Notification], int]]:
        """Once the daemon has stopped: the notifications it held then and the next ID it would have
        given, or None when it handed nothing over."""
        if self._thread is not None:
            self._stopped.set()
            self._thread.join(HANDOVER_TIMEOUT)
        self.close()
        try:
            held = json.loads(self._data.decode('utf-8'))
            notifications = [Notification.make(dct) for dct in held['notifications']]
            next_id = held['next_id']
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if not isinstance(next_id, int) or not all(isinstance(ntf.id, int) for ntf in notifications):
            return None
        return notifications, next_id


class Handover:
    """What a daemon takes over from the daemons it replaced: the final queue each one handed over,
    or else the queue it saved."""

    def __init__(self) -> None:
        self.replaced: bool = False
        self._sources: List[Union[str, Tuple[List[Notification], int]]] = []
        self._handed_over: Set[str] = set()

    def add_saved(self, queue_file: str) -> None:
        self.replaced = True
        self._sources.append(queue_file)

    def add_held(self, queue_file: Optional[str], queue: Tuple[List[Notification], int]) -> None:
        self.replaced = True
        self._sources.append(queue)
        if queue_file:
            # the saved queue only has part of what was handed over
            self._handed_over.add(os.path.realpath(queue_file))

    def sources(self, queue_file: str) -> List[Union[str, Tuple[List[Notification], int]]]:
        """What the new queue is made of, in order: what the replaced daemons held, then the saved
        queue of this daemon, unless a replaced daemon handed over everything it had."""
        sources = [source for source in self._sources
                   if not (isinstance(source, str) and os.path.realpath(source) in self._handed_over)]
        if os.path.realpath(queue_file) not in self._handed_over:
            sources.append(queue_file)
        return sources


class DaemonInstance:
    """Keeps to one daemon per runtime directory and per saved queue, and looks after the socket in
    the runtime directory and the link at the old shared path."""

    def __init__(self, queue_file: str) -> None:
        self.queue_file: str = queue_file
        self.runtime_dir, self._fallback = runtime_dir()
        self.socket_path: str = os.path.join(self.runtime_dir, SOCKET_NAME)
        self._locks = (_InstanceLock(self.socket_path + '.lock'),
                       _InstanceLock(os.path.splitext(queue_file)[0] + '.lock'))
        # names the private channel a daemon that replaces this one listens on
        self._handover_token: str = secrets.token_hex(16)

    def _check_runtime_dir(self) -> None:
        if not self._fallback:
            if not os.path.isdir(self.runtime_dir):
                raise StartError(f'the runtime directory {self.runtime_dir} is not an existing directory')
            return
        try:
            st = os.lstat(self.runtime_dir)
        except FileNotFoundError:
            return
        except OSError as error:
            raise StartError(f'cannot use {self.runtime_dir}: {error.strerror}')
        if not _own_directory(st):
            raise StartError(f'{self.runtime_dir} is not a directory that belongs to this user')

    def _open_runtime_dir(self) -> int:
        flags = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            try:
                return os.open(self.runtime_dir, os.O_RDONLY | flags)
            except PermissionError:
                # a directory without read permission for its owner can still be checked and changed
                return os.open(self.runtime_dir, os.O_PATH | flags)
        except OSError as error:
            raise StartError(f'cannot use {self.runtime_dir}: {error.strerror}')

    def _set_runtime_dir_mode(self, fd: int, mode: int) -> None:
        try:
            try:
                os.fchmod(fd, mode)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
                # an O_PATH descriptor: change the directory it was opened on
                os.chmod(f'/proc/self/fd/{fd}', mode)
        except OSError as error:
            raise StartError(f'cannot change the mode of {self.runtime_dir}: {error.strerror}')

    def _secure_runtime_dir(self) -> Optional[int]:
        """Create the fallback directory, or give an existing one of this user mode 0700, whatever
        bits it had. Returns the mode an existing directory had, when it was changed."""
        try:
            os.mkdir(self.runtime_dir, 0o700)
            created = True
        except FileExistsError:
            created = False
        except OSError as error:
            raise StartError(f'cannot create {self.runtime_dir}: {error.strerror}')
        fd = self._open_runtime_dir()
        try:
            st = os.fstat(fd)
            if not _own_directory(st):
                raise StartError(f'{self.runtime_dir} is not a directory that belongs to this user')
            mode = stat.S_IMODE(st.st_mode)
            if mode == 0o700:
                return None
            self._set_runtime_dir_mode(fd, 0o700)
            return None if created else mode
        finally:
            os.close(fd)

    def _restore_runtime_dir(self, mode: int) -> None:
        """Give the fallback directory back the mode it had before a start that didn't go through."""
        try:
            fd = self._open_runtime_dir()
        except StartError:
            return
        try:
            if _own_directory(os.fstat(fd)):
                self._set_runtime_dir_mode(fd, mode)
        except (OSError, StartError):
            pass
        finally:
            os.close(fd)

    def _runtime_dir_searchable(self) -> bool:
        try:
            return bool(os.lstat(self.runtime_dir).st_mode & stat.S_IXUSR)
        except FileNotFoundError:
            return True

    def claim(self, replace: bool = False) -> Handover:
        """Make this process the daemon. Until that is settled nothing is created or changed, and the
        saved queue isn't read. Raises StartError when another daemon runs, or the runtime directory
        can't be used."""
        self._check_runtime_dir()
        holders = {}
        for lock in self._locks:
            if lock is self._locks[0] and self._fallback and not self._runtime_dir_searchable():
                # Nothing in a fallback directory its owner can't search can be looked at without
                # changing its mode first: its lock is checked when it is taken, below.
                continue
            pid = lock.holder()
            if pid is not None:
                holders.setdefault(pid, []).append(lock)
        handover = Handover()
        if holders:
            if not replace:
                raise StartError(f'a daemon is already running ({_describe(holders)})')
            handover = self._replace(holders)

        previous_mode = self._secure_runtime_dir() if self._fallback else None
        try:
            try:
                os.makedirs(os.path.dirname(self.queue_file), exist_ok=True)
            except OSError as error:
                raise StartError(f'cannot create {os.path.dirname(self.queue_file)}: {error.strerror}')
            for lock in self._locks:
                if not lock.acquire():
                    # Another daemon started meanwhile and keeps the mode just set: leave it as it is.
                    previous_mode = None
                    raise StartError(f'a daemon is already running ({_describe([lock.holder()])})')
        except StartError:
            if previous_mode is not None:
                self._restore_runtime_dir(previous_mode)
            raise
        info = {'pid': os.getpid(), 'socket': os.path.abspath(self.socket_path),
                'queue': os.path.abspath(self.queue_file), 'handover': self._handover_token}
        for lock in self._locks:
            lock.write_info(info)
        return handover

    def _replace(self, holders: Mapping[int, Sequence[_InstanceLock]]) -> Handover:
        infos = {}
        for pid, locks in holders.items():
            info = self._read_info(pid, locks) if pid > 0 else {}
            if info.get('pid') != pid:
                # The kernel names no process (pid 0: another pid namespace or a network file system),
                # or not the daemon that wrote the lock file: there is nothing safe to signal.
                raise StartError(f'a daemon is already running ({_describe([pid])}), and it cannot be '
                                 f'stopped from here')
            infos[pid] = info
        replaced = []
        try:
            for pid, locks in holders.items():
                info = infos[pid]
                token = info.get('handover')
                # listen before the daemon is told to stop: it hands over when it stops
                receiver = _HandoverReceiver(token, pid) if isinstance(token, str) and token else None
                queue_file = info.get('queue') if isinstance(info.get('queue'), str) else None
                replaced.append((queue_file, receiver))
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise StartError(f'cannot stop the running daemon (pid {pid}): it belongs to another user')
            deadline = time.monotonic() + REPLACE_TIMEOUT
            while any(lock.holder() is not None for locks in holders.values() for lock in locks):
                if time.monotonic() >= deadline:
                    raise StartError(f'the running daemon ({_describe(holders)}) did not stop within '
                                     f'{REPLACE_TIMEOUT} seconds')
                time.sleep(0.02)
            handover = Handover()
            for queue_file, receiver in replaced:
                queue = receiver.queue() if receiver else None
                if queue is not None:
                    handover.add_held(queue_file, queue)
                elif queue_file:
                    # it stopped without handing over: what it saved is what is left of its queue
                    handover.add_saved(queue_file)
            return handover
        finally:
            for _, receiver in replaced:
                if receiver:
                    receiver.close()

    def hand_over(self, queue: NotificationQueue) -> None:
        """Give a daemon that replaces this one everything the queue still holds, transient
        notifications included, and the next ID. Call it once nothing changes the queue anymore. Does
        nothing when no daemon replaces this one."""
        with queue.lock:
            data = json.dumps({'next_id': queue.next_id, 'notifications': list(queue)},
                              default=Notification.asdict).encode('utf-8')
        try:
            with connect_own(_handover_address(self._handover_token), timeout=HANDOVER_TIMEOUT) as sck:
                sck.sendall(data)
        except OSError:
            pass

    @staticmethod
    def _read_info(pid: int, locks: Sequence[_InstanceLock]) -> Mapping[str, any]:
        # A daemon writes this right after taking its locks.
        deadline = time.monotonic() + 1
        while True:
            for lock in locks:
                info = lock.read_info()
                if info.get('pid') == pid:
                    return info
            if time.monotonic() >= deadline:
                return {}
            time.sleep(0.02)

    def link_legacy(self) -> Optional[str]:
        """Point the old shared path at the socket in the runtime directory, when the path is missing,
        a link of this user, or a socket of this user nothing listens on. Anything else stays as it is,
        and the returned message says so."""
        legacy = _static.ROFICATION_UNIX_SOCK
        try:
            st = os.lstat(legacy)
        except FileNotFoundError:
            st = None
        except OSError as error:
            return f'left {legacy} as it is: {error.strerror}'
        if st is not None:
            # a path of another user is never followed or connected to
            replaceable = st.st_uid == os.getuid() and (
                stat.S_ISLNK(st.st_mode) or (stat.S_ISSOCK(st.st_mode) and not _is_listening(legacy)))
            if not replaceable:
                return f'left {legacy} as it is: it is in use or not ours, so it does not lead to this daemon'
            try:
                os.unlink(legacy)
            except FileNotFoundError:
                pass
            except OSError as error:
                return f'left {legacy} as it is: {error.strerror}'
        try:
            os.symlink(os.path.abspath(self.socket_path), legacy)
        except OSError as error:
            return f'could not link {legacy} to this daemon: {error.strerror}'
        return None

    def unlink_legacy(self) -> None:
        """Remove the link at the old shared path, if it is still this user's link to this daemon."""
        legacy = _static.ROFICATION_UNIX_SOCK
        try:
            st = os.lstat(legacy)
            if stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid() \
                    and os.readlink(legacy) == os.path.abspath(self.socket_path):
                os.unlink(legacy)
        except OSError:
            pass
