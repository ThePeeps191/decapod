"""Run untrusted Python in a constrained subprocess.

Isolation levels:

* ``bwrap`` – bubblewrap: read-only system, the instance directory and home are masked,
  only the sandbox folder is writable, network unshared unless allowed. Linux only.
* ``none`` – a plain subprocess with a scrubbed environment and resource limits. This is
  NOT a security boundary; the operator must opt in to it explicitly.

With ``auto`` (the default) ``code.run`` is only available when bubblewrap is installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from decapod.tools.base import ProcessRegistry


@dataclass
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    isolation: str


def bwrap_path() -> str | None:
    if sys.platform != "linux":
        return None
    return shutil.which("bwrap")


def effective_isolation(requested: str) -> str | None:
    """Resolve the isolation level, or None if the request cannot be satisfied."""
    if requested == "none":
        return "none"
    if bwrap_path():
        return "bwrap"
    return None


def _limits(memory_mb: int, cpu_s: int) -> None:  # pragma: no cover - runs in the child
    import resource

    mem = memory_mb * 1024 * 1024
    for limit, value in (
        (resource.RLIMIT_AS, mem),
        (resource.RLIMIT_CPU, cpu_s),
        (resource.RLIMIT_FSIZE, 50 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ):
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            pass


async def run_python(
    code: str,
    *,
    workdir: Path,
    instance_root: Path,
    timeout_s: float,
    memory_mb: int = 512,
    allow_network: bool = False,
    isolation: str = "auto",
    procs: ProcessRegistry | None = None,
    max_output: int = 20_000,
) -> SandboxResult:
    level = effective_isolation(isolation)
    if level is None:
        raise RuntimeError(
            "code.run needs bubblewrap (bwrap) for isolation; install it or set "
            'tools.code.isolation = "none" to accept an unisolated subprocess'
        )
    workdir.mkdir(parents=True, exist_ok=True)
    fd, script_name = tempfile.mkstemp(prefix="run_", suffix=".py", dir=workdir)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code)
    script = Path(script_name)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")

    python = sys.executable
    argv = [python, "-I", "-B", str(script)]
    if level == "bwrap":
        bwrap = bwrap_path()
        assert bwrap is not None
        masks: list[str] = []
        home = Path.home().resolve()
        root = instance_root.resolve()
        for masked in (home, root):
            if masked == Path("/"):
                continue
            if masked != home and home in masked.parents:
                continue  # already hidden by the home mask
            masks += ["--tmpfs", str(masked)]
        # The interpreter may live under a masked directory (e.g. a venv in $HOME):
        # re-expose just the Python installation, read-only.
        runtime_dirs = {
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            Path(os.path.realpath(sys.executable)).parent,
        }
        for runtime in sorted(runtime_dirs):
            if any(m == runtime or m in runtime.parents for m in (home, root)):
                masks += ["--ro-bind", str(runtime), str(runtime)]
        argv = [
            bwrap,
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            *masks,
            "--bind",
            str(workdir.resolve()),
            str(workdir.resolve()),
            "--chdir",
            str(workdir.resolve()),
            "--unshare-all",
            *(["--share-net"] if allow_network else []),
            "--die-with-parent",
            "--new-session",
            *argv,
        ]

    kwargs: dict[str, object] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
        cpu = max(1, int(timeout_s) + 1)
        kwargs["preexec_fn"] = lambda: _limits(memory_mb, cpu)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(workdir),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,  # type: ignore[arg-type]
    )
    if procs is not None:
        procs.add(proc)
    timed_out = False
    try:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            _kill(proc)
            stdout, stderr = await proc.communicate()
    finally:
        if procs is not None:
            procs.discard(proc)
        if proc.returncode is None:
            _kill(proc)
        script.unlink(missing_ok=True)
    return SandboxResult(
        exit_code=proc.returncode,
        stdout=stdout.decode("utf-8", errors="replace")[:max_output],
        stderr=stderr.decode("utf-8", errors="replace")[:max_output],
        timed_out=timed_out,
        isolation=level,
    )


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform != "win32":
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
