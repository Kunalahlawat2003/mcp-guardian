"""Process management: single-instance lock and launching the daemon in the background."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import IO

from mcp_guardian import paths


class SingleInstanceLock:
    """An OS-level exclusive lock on ``daemon.lock``; released automatically if the process dies."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> bool:
        fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def _python_for_background() -> str:
    """On Windows prefer pythonw.exe so no console window flashes up."""
    exe = sys.executable
    if os.name == "nt":
        candidate = Path(exe).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return exe


def spawn_daemon(home: Path, ui: str | None = None) -> subprocess.Popen[bytes]:
    """Start ``mcp-guardian daemon`` detached from the caller (survives Claude restarts)."""
    paths.ensure_private_dir(home)
    paths.log_dir(home).mkdir(parents=True, exist_ok=True)
    args = [_python_for_background(), "-m", "mcp_guardian", "daemon", "--detached"]
    if ui:
        args += ["--ui", ui]
    env = dict(os.environ, MCP_GUARDIAN_HOME=str(home))
    log_file = open(paths.daemon_log_path(home), "ab")
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": log_file,
        "env": env,
        "cwd": str(home),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(args, **kwargs)
    finally:
        log_file.close()


def read_pid(home: Path) -> int | None:
    try:
        return int(paths.pid_path(home).read_text().strip())
    except (OSError, ValueError):
        return None
