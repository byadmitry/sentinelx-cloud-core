"""Process-creation flags, so nothing we spawn flashes a window on Windows.

On Windows a console application launched from a process that has no console
of its own gets a brand-new one allocated -- and that console comes with a
VISIBLE window. When the agent runs as a service or from a pythonw-based
scheduled task it has no console, so every child briefly painted a black box
on the operator's desktop: exec, git, project_snapshot, edit, local_api. One
operator counted several flashes in a row during ordinary work.

CREATE_NO_WINDOW gives the child its own console WITHOUT a window, which is
both the fix and, as handlers/script.py already relied on, what guarantees the
child has a console at all when the agent runs as a service.

This lives in one place on purpose. The flag was already applied correctly in
script.py and nowhere else; seven other spawn sites had grown up without it,
which is exactly what happens when the knowledge lives in one handler's
comments instead of in a shared helper.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# winbase.h. Not imported from subprocess because that constant only exists on
# Windows builds, and this module is imported everywhere.
CREATE_NO_WINDOW = 0x08000000


def _shed_agent_priority() -> None:
    """In the child, before exec: give back the priority systemd grants the agent.

    The unit runs the agent with Nice=-5 and OOMScoreAdjust=-500 so it stays
    reachable on a loaded host and isn't what the kernel kills when memory runs
    out. Children inherit both, so a user's workload started through SentinelX
    got the agent's protection: sxrep_E6VEFQ602FAF, ~20 GB of project processes
    that the OOM killer would spare while killing everything else first. Both
    go back to neutral here. Raising one's own nice value and oom_score_adj
    needs no privilege; only lowering them does, so this can't fail for lack of
    rights, and it only touches values below zero (an operator who set a
    positive nice for the agent keeps it for its children too).

    preexec_fn runs between fork and exec, where Python warns that locks held by
    other threads can deadlock. This sticks to plain syscalls (getpriority,
    setpriority, open/read/write/close on a file descriptor), no buffered I/O
    and no logging; glibc resets its malloc locks in the child.
    """
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < 0:
            os.setpriority(os.PRIO_PROCESS, 0, 0)
    except OSError:
        pass
    if sys.platform.startswith("linux"):
        try:
            fd = os.open("/proc/self/oom_score_adj", os.O_RDWR)
            try:
                if os.read(fd, 16).strip().startswith(b"-"):
                    os.write(fd, b"0")
            finally:
                os.close(fd)
        except OSError:
            pass


def spawn_kwargs(**extra: Any) -> dict[str, Any]:
    """Keyword arguments for create_subprocess_exec / Popen.

    On Windows adds CREATE_NO_WINDOW, merging rather than overwriting any
    creationflags the caller already passed. Elsewhere adds a preexec_fn that
    drops the agent's raised priority and OOM protection in the child (see
    _shed_agent_priority), chained with any preexec_fn the caller passed. A
    caller can use this unconditionally.
    """
    kwargs = dict(extra)
    if sys.platform == "win32":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    else:
        caller_preexec = kwargs.get("preexec_fn")
        if caller_preexec is None:
            kwargs["preexec_fn"] = _shed_agent_priority
        else:
            def _both() -> None:
                _shed_agent_priority()
                caller_preexec()
            kwargs["preexec_fn"] = _both
    return kwargs
