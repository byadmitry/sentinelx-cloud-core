"""The update playbooks shipped in the Linux config must work on a standard install.

2026-10-07, sxrep_JYGA3J9A3Z9S: update_sentinelx_code ran git fetch / git pull in
/opt/sentinelx-cloud-core as the agent's user, and sentinelx_meta fetched there
for its update check. The standard install keeps that checkout owned by root, so
neither could ever work, and an assistant tried changing ownership to make it.
"""

from __future__ import annotations

import pathlib
import re

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
_GIT_IN_CHECKOUT = re.compile(
    r"cd /opt/sentinelx-cloud-core\s*&&\s*git\b|git -C /opt/sentinelx-cloud-core\b")


def _playbooks() -> dict:
    return yaml.safe_load((ROOT / "config.example.yaml").read_text())["playbooks"]


def test_the_update_playbook_uses_the_supported_updater():
    steps = " ".join(_playbooks()["update_sentinelx_code"]["steps"])
    assert "sentinel_agent_update" in steps
    assert "not_confirmed" in steps  # wait, don't retry, when it can't confirm yet


def test_no_linux_playbook_runs_git_inside_the_root_owned_checkout():
    for name, pb in _playbooks().items():
        for step in pb.get("steps", []):
            assert not _GIT_IN_CHECKOUT.search(step), (name, step[:100])
