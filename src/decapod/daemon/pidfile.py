"""PID file handling and cross-platform process checks."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        # If it is our own exited child, reap it so it does not linger as a zombie.
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = Path(f"/proc/{pid}/status")
    if status.exists():  # Linux: a zombie is not alive
        with contextlib.suppress(OSError):
            for line in status.read_text().splitlines():
                if line.startswith("State:"):
                    return "Z" not in line.split(":", 1)[1].split()[0]
    return True


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _locked_by_other(path: Path) -> bool | None:
    """POSIX: True if another process holds the daemon's lock on ``path``.

    None when locking is unavailable (Windows) and the caller must fall back to PID checks.
    The lock, not the PID, is the source of truth: it dies with the process, so a stale PID
    file whose number was reused by an unrelated process is never mistaken for a daemon.
    """
    if sys.platform == "win32":
        return None
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def running_pid(path: Path) -> int | None:
    locked = _locked_by_other(path)
    if locked is False:
        return None
    pid = read_pid(path)
    if pid is not None and (locked or pid_alive(pid)):
        return pid
    return None


def terminate(pid: int, *, force: bool = False) -> None:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, sig)


class PidFile:
    """Exclusive, lifetime-long lock on the PID file (``flock`` on POSIX)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            existing = running_pid(self.path)
            if existing is not None and existing != os.getpid():
                raise AlreadyRunning(
                    f"another Decapod daemon is running for this instance (pid {existing})"
                )
            self.path.write_text(str(os.getpid()), encoding="utf-8")
            self.acquired = True
            return
        import fcntl

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            existing = read_pid(self.path)
            raise AlreadyRunning(
                "another Decapod daemon is running for this instance"
                + (f" (pid {existing})" if existing else "")
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        self._fd = fd
        self.acquired = True

    def release(self) -> None:
        if self.acquired and read_pid(self.path) == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)  # closing releases the lock
            self._fd = None
        self.acquired = False

    def __enter__(self) -> PidFile:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
