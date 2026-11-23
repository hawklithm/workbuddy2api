"""Process-wide and cross-process locks for the account storage directory."""
import os
from pathlib import Path
import threading
import time

_guard = threading.Lock()
_locks = {}


class SharedDirectoryLock:
    def __init__(self, directory: Path):
        # Keep the legacy lock name so old and renamed clients still coordinate.
        self.path = directory / '.workbuddy-auth.lock'
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            depth = getattr(self._local, 'depth', 0)
            if not depth:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                f = self.path.open('a+b')
                try:
                    if os.name == 'nt':
                        import msvcrt
                        # LockFile can lock a byte beyond EOF. Do not read or
                        # write byte zero before acquiring it: Windows rejects
                        # those operations while another process owns the lock.
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                                break
                            except OSError:
                                if time.monotonic() >= deadline:
                                    raise TimeoutError("timed out waiting for account storage lock") from None
                                time.sleep(0.05)
                    else:
                        import fcntl
                        fcntl.flock(f, fcntl.LOCK_EX)
                except BaseException:
                    f.close()
                    raise
                self._local.file = f
            self._local.depth = depth + 1
            return self
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, *args):
        try:
            depth = self._local.depth - 1
            self._local.depth = depth
            if depth == 0:
                f = self._local.file
                try:
                    if os.name == 'nt':
                        import msvcrt
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(f, fcntl.LOCK_UN)
                finally:
                    f.close()
                    del self._local.file
        finally:
            self._thread_lock.release()


def directory_lock(directory: Path) -> SharedDirectoryLock:
    directory = Path(directory).expanduser().resolve()
    key = os.path.normcase(os.fspath(directory))
    with _guard:
        if key not in _locks:
            _locks[key] = SharedDirectoryLock(directory)
        return _locks[key]
