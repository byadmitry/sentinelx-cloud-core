"""exec timeout/cancellation must not leak descendants from bash -lc."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
import time

import pytest

from sentinelx_core.executor_engine import run_shell, run_shell_split

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


def _group_members(pgid: int) -> list[str]:
    out = subprocess.run(
        ["ps", "-eo", "pgid=,stat=,pid=,comm="], capture_output=True, text=True, check=True
    ).stdout
    members = []
    for line in out.splitlines():
        fields = line.split()
        if fields and fields[0] == str(pgid) and not fields[1].startswith("Z"):
            members.append(line.strip())
    return members


def _wait_group_gone(pgid: int, timeout: float = 3.0) -> list[str]:
    deadline = time.monotonic() + timeout
    members = _group_members(pgid)
    while members and time.monotonic() < deadline:
        time.sleep(0.05)
        members = _group_members(pgid)
    return members


def _tree_command(pidfile) -> str:
    return (
        f"printf '%s' \"$$\" > {shlex.quote(str(pidfile))}; "
        "sleep 30 & sleep 30"
    )


@pytest.mark.parametrize("runner", [run_shell, run_shell_split])
async def test_exec_timeout_kills_the_whole_shell_process_group(tmp_path, runner):
    pidfile = tmp_path / "pgid"
    result = await runner(_tree_command(pidfile), timeout=0.25)

    assert result.get("returncode") == -1
    pgid = int(pidfile.read_text())
    assert _wait_group_gone(pgid) == []


async def test_exec_cancellation_kills_the_whole_shell_process_group(tmp_path):
    pidfile = tmp_path / "pgid"
    task = asyncio.create_task(run_shell(_tree_command(pidfile), timeout=30))

    deadline = time.monotonic() + 3
    while not pidfile.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert pidfile.exists(), "shell did not start"
    pgid = int(pidfile.read_text())
    assert _group_members(pgid), "fixture should have a live process group"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _wait_group_gone(pgid) == []
