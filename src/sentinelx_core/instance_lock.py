"""One running agent per identity on a machine.

Two agents connecting with the same identity drop each other on every connect:
the hub keeps the newest session and closes the other, which reconnects at
once. On a Windows service install, restarts could leave the previous agent
running (the venv's python.exe is a launcher that starts the real interpreter
as a child, which could outlive the service's own process), and win-acer ran
~44,000 such reconnects in 2.5 hours before anyone noticed (sxrep_NVKZAMSWX6XJ).

At startup the agent takes an exclusive OS lock and holds it for its whole
life; a second instance for the same host finds it taken and exits instead of
connecting. The OS releases the lock when the process ends, however it ends,
so a crash never leaves it stuck, and a supervisor that retries (WinSW,
systemd) takes over on its own once the other process is gone.

The lock is per host id, in the directory the agent already owns for its
rotated credential: two different agents on one machine (a prod and a dev
agent, say) never block each other.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import IO

from sentinelx_core import rotation


class AlreadyRunning(Exception):
    def __init__(self, path: Path, holder: str | None):
        super().__init__(f"agent lock {path} is held (pid {holder or '?'})")
        self.path = path
        self.holder = holder


def lock_path(identity_path: Path, host_id: str) -> Path | None:
    """Where this host's lock lives, or None if no writable directory exists."""
    rp = rotation.rotated_path(identity_path)
    if rp is None:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", host_id or "unknown")
    return rp.parent / f"agent-{safe}.lock"


def _lock(fh: IO[str]) -> None:
    """Take the lock without waiting; OSError if another process holds it."""
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _holder(path: Path) -> str | None:
    try:
        text = path.read_text().strip()
    except OSError:  # on Windows the locked byte can't be read by others
        return None
    return text if text.isdigit() else None


def acquire(identity_path: Path, host_id: str) -> IO[str] | None:
    """Hold this host's lock for the life of the process.

    Returns the open file (keep a reference: closing it releases the lock), or
    None when no writable directory exists, in which case the agent runs
    unprotected rather than not at all. Raises AlreadyRunning if another
    process holds it.
    """
    path = lock_path(identity_path, host_id)
    if path is None:
        return None
    fh = open(path, "a+", encoding="utf-8")  # "a+": never truncate before locking
    try:
        _lock(fh)
    except OSError:
        fh.close()
        raise AlreadyRunning(path, _holder(path)) from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


EXIT_ALREADY_RUNNING = 3  # non-zero on purpose: WinSW and systemd retry, and
                          # this instance takes over once the other one is gone


def hold_or_exit(identity_path: Path, host_id: str, log) -> IO[str] | None:
    """Take this host's lock or end the process with a clear log line."""
    try:
        held = acquire(identity_path, host_id)
    except AlreadyRunning as exc:
        log.error(
            "another SentinelX agent is already running for host %s (lock %s, pid %s); "
            "exiting so the two don't keep dropping each other. If that process "
            "shouldn't be running, stop it and this agent will start.",
            host_id, exc.path, exc.holder or "?",
        )
        sys.exit(EXIT_ALREADY_RUNNING)
    if held is None:
        log.warning("no writable state directory; running without the single-instance lock")
    return held
