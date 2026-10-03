"""Children don't inherit the agent's raised priority (sxrep_E6VEFQ602FAF).

systemd runs the agent with Nice=-5 and OOMScoreAdjust=-500; everything it
started inherited both, so ~20 GB of a user's project processes were spared by
the OOM killer. Each test runs a small "agent" process that gives itself the
unit's values and then starts a child, which reports its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or os.geteuid() != 0,
    reason="needs Linux and root to give the stand-in agent a negative nice and oom_score_adj",
)

REPORT = "import os; print(os.getpriority(os.PRIO_PROCESS, 0), open('/proc/self/oom_score_adj').read().strip())"


def _run_as_agent(spawn: str, nice: int = -5, oom: int = -500) -> str:
    """Start a stand-in agent with the unit's priority; return what its child reports."""
    script = textwrap.dedent(f"""
        import asyncio, os, subprocess, sys
        from sentinelx_core.winspawn import spawn_kwargs
        os.setpriority(os.PRIO_PROCESS, 0, {nice})
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("{oom}")
        child = [sys.executable, "-c", {REPORT!r}]
        {spawn}
    """)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


SUBPROCESS = "print(subprocess.run(child, capture_output=True, text=True, **spawn_kwargs()).stdout.strip())"
ASYNCIO = textwrap.dedent("""
async def main():
    p = await asyncio.create_subprocess_exec(*child, stdout=subprocess.PIPE, **spawn_kwargs())
    print((await p.communicate())[0].decode().strip())
asyncio.run(main())
""").replace("\n", "\n        ")
WITHOUT = "print(subprocess.run(child, capture_output=True, text=True).stdout.strip())"


def test_a_child_started_through_spawn_kwargs_is_back_to_neutral():
    assert _run_as_agent(SUBPROCESS) == "0 0"


def test_the_asyncio_path_used_by_exec_too():
    assert _run_as_agent(ASYNCIO) == "0 0"


def test_control_without_spawn_kwargs_the_child_inherits():
    # Proves the stand-in reproduces the problem.
    assert _run_as_agent(WITHOUT) == "-5 -500"


def test_a_positive_nice_chosen_by_the_operator_is_kept():
    assert _run_as_agent(SUBPROCESS, nice=5, oom=0) == "5 0"


def test_a_callers_preexec_fn_still_runs(tmp_path):
    marker = tmp_path / "ran"
    spawn = (f"print(subprocess.run(child, capture_output=True, text=True, "
             f"**spawn_kwargs(preexec_fn=lambda: open({str(marker)!r}, 'w').close())).stdout.strip())")
    assert _run_as_agent(spawn) == "0 0"
    assert marker.exists()
