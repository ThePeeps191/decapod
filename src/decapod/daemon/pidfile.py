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


def running_pid(path: Path) -> int | None:
    pid = read_pid(path)
    if pid is not None and pid_alive(pid):
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
    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def acquire(self) -> None:
        existing = running_pid(self.path)
        if existing is not None and existing != os.getpid():
            raise AlreadyRunning(
                f"another Decapod daemon is running for this instance (pid {existing})"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".pid.tmp")
        tmp.write_text(str(os.getpid()), encoding="utf-8")
        tmp.replace(self.path)
        self.acquired = True

    def release(self) -> None:
        if self.acquired and read_pid(self.path) == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()
        self.acquired = False

    def __enter__(self) -> PidFile:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
