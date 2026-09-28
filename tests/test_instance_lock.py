"""One running agent per host on a machine (sxrep_NVKZAMSWX6XJ).

Orphaned agent processes on a Windows service install kept connecting with the
same identity, and win-acer dropped and reconnected ~44,000 times in 2.5 hours.
A second instance must now find the lock taken and exit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from sentinelx_core import instance_lock as L
from sentinelx_core import rotation

IDENT = Path("/nonexistent/identity.json")  # only the state dir matters here


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setattr(rotation, "_DIR_CANDIDATES", (str(d),))
    return d


def test_the_first_agent_gets_the_lock_and_records_its_pid(state_dir):
    fh = L.acquire(IDENT, "host_a")
    assert fh is not None
    assert (state_dir / "agent-host_a.lock").read_text() == str(os.getpid())


def test_a_second_instance_for_the_same_host_is_refused(state_dir):
    held = L.acquire(IDENT, "host_a")
    with pytest.raises(L.AlreadyRunning) as err:
        L.acquire(IDENT, "host_a")
    assert err.value.holder == str(os.getpid())
    held.close()


def test_different_hosts_never_block_each_other(state_dir):
    # e.g. the prod and the dev agent on one machine, sharing the state dir.
    # Keep both handles: a dropped handle closes its file and releases the lock.
    prod = L.acquire(IDENT, "host_prod")
    dev = L.acquire(IDENT, "dev-orion-b3d403faf023")
    assert prod is not None and dev is not None


def test_the_os_releases_the_lock_when_the_holder_is_killed(state_dir):
    src = str(Path(L.__file__).parents[1])
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {src!r})
        from pathlib import Path
        from sentinelx_core import rotation, instance_lock as L
        rotation._DIR_CANDIDATES = ({str(state_dir)!r},)
        fh = L.acquire(Path("/nonexistent/identity.json"), "host_a")
        print("held", flush=True)
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(L.AlreadyRunning):
            L.acquire(IDENT, "host_a")
    finally:
        proc.kill()  # no clean shutdown: the crash case
        proc.wait()
    assert L.acquire(IDENT, "host_a") is not None


def test_no_writable_directory_means_running_unprotected(monkeypatch, tmp_path):
    monkeypatch.setattr(rotation, "_DIR_CANDIDATES", (str(tmp_path / "missing"),))
    assert L.acquire(IDENT, "host_a") is None


def test_odd_host_ids_make_safe_file_names(state_dir):
    assert L.acquire(IDENT, "host_d08a │ x/y") is not None
    assert (state_dir / "agent-host_d08a___x_y.lock").exists()


def test_a_duplicate_exits_with_a_clear_message(state_dir):
    held = L.acquire(IDENT, "host_a")
    logged = []

    class Log:
        def error(self, msg, *a):
            logged.append(msg % a)

        def warning(self, msg, *a):
            logged.append(msg % a)

    with pytest.raises(SystemExit) as ex:
        L.hold_or_exit(IDENT, "host_a", Log())
    # Literal 3, not the constant: a zero exit would stop WinSW and systemd from
    # retrying, and the agent would never take over once the other one is gone.
    assert ex.value.code == 3
    assert "already running for host host_a" in logged[0]
    held.close()


def test_windows_takes_a_non_blocking_msvcrt_lock(state_dir, monkeypatch):
    calls = []

    def locking(fd, mode, nbytes):
        # where the lock sits: past the pid, so a refused instance can read it
        calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if len(calls) > 1:
            raise OSError("locked")

    monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_NBLCK=2, locking=locking))
    monkeypatch.setattr(sys, "platform", "win32")
    assert L.acquire(IDENT, "host_w") is not None
    with pytest.raises(L.AlreadyRunning):
        L.acquire(IDENT, "host_w")
    assert calls == [(2, 1, L._WIN_LOCK_OFFSET), (2, 1, L._WIN_LOCK_OFFSET)]
    assert L._WIN_LOCK_OFFSET > 64  # well past any pid written at offset 0


def test_verify_enrollment_runs_without_taking_the_lock():
    # --verify-enrollment is documented as safe to run while the agent runs,
    # so it must exit before the lock is taken.
    src = (Path(L.__file__).parent / "__main__.py").read_text()
    assert src.index("if args.verify_enrollment:") < src.index("hold_or_exit(")
