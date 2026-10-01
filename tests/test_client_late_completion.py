"""A result that finishes after the new connection's opening replay is delivered.

Issue #38 (FalconZip): a background job sends its completion on the socket it
started on. If that socket died and the replacement connection has already run
its one opening replay, the result sat on disk until the NEXT disconnect.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sentinelx_core import client as CL
from sentinelx_core import pending_results as pr
from sentinelx_core.client import HubClient


def _client(tmp_path: Path) -> HubClient:
    c = HubClient.__new__(HubClient)
    up = tmp_path / "uploads"
    up.mkdir()
    ex = MagicMock()
    ex.upload_base = up
    ex.dispatch = AsyncMock(return_value={"ok": True, "output": "done"})
    c._executor = ex
    c._identity = SimpleNamespace(host_id="host_x")
    return c


def _dead_socket():
    ws = AsyncMock()
    ws.send.side_effect = ConnectionError("socket closed")
    return ws


def _sent_job_ids(ws):
    return [json.loads(call.args[0])["data"]["job_id"] for call in ws.send.await_args_list]


async def test_a_late_completion_goes_out_on_the_live_connection_at_once(tmp_path):
    # FalconZip's case: new connection up (its opening replay already done),
    # then the old job finishes and its own socket is dead.
    c = _client(tmp_path)
    live = AsyncMock()
    c._result_state()["current_ws"] = live
    await c._run_job_and_report(_dead_socket(), SimpleNamespace(op="exec"), "job_late",
                                datetime.now(timezone.utc))
    assert _sent_job_ids(live) == ["job_late"]
    assert list(pr.drain(c._executor.upload_base)) == []      # nothing left behind


async def test_without_a_live_connection_the_result_waits_on_disk(tmp_path):
    c = _client(tmp_path)
    await c._run_job_and_report(_dead_socket(), SimpleNamespace(op="exec"), "job_wait",
                                datetime.now(timezone.utc))
    held = list(pr.drain(c._executor.upload_base))
    assert len(held) == 1 and CL._held_job_id(held[0][1]) == "job_wait"


async def test_the_heartbeat_delivers_what_is_held(tmp_path, monkeypatch):
    c = _client(tmp_path)
    pr.record(c._executor.upload_base, "job_held",
              {"kind": "job_completed", "data": {"job_id": "job_held", "status": "succeeded"}})
    monkeypatch.setattr(CL, "HEARTBEAT_INTERVAL_SECONDS", 0)
    ws = AsyncMock()
    ws.send.side_effect = [None, None, ConnectionError("stop the loop")]  # ping, replay, next ping
    with pytest.raises(ConnectionError):
        await c._heartbeat_loop(ws)
    sent = [json.loads(call.args[0]) for call in ws.send.await_args_list[:2]]
    assert any(m.get("data", {}).get("job_id") == "job_held" for m in sent)
    assert list(pr.drain(c._executor.upload_base)) == []


async def test_a_result_its_own_job_is_sending_is_not_replayed_too(tmp_path):
    c = _client(tmp_path)
    pr.record(c._executor.upload_base, "job_busy",
              {"kind": "job_completed", "data": {"job_id": "job_busy", "status": "succeeded"}})
    c._result_state()["sending"].add("job_busy")
    ws = AsyncMock()
    await c._replay_pending_results(ws)
    ws.send.assert_not_awaited()
    assert len(list(pr.drain(c._executor.upload_base))) == 1   # still there for later


def test_held_job_id_reads_the_real_event_shape():
    assert CL._held_job_id({"kind": "job_completed", "data": {"job_id": "j1"}}) == "j1"
