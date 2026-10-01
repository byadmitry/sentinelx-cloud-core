"""Reads never starve behind scans; scans never outlive the call (sxrep_TCWAAH5ATMFH).

read, list and search shared asyncio's default thread pool, and a recursive
search had no time limit. Threads can't be cancelled, so searches the hub had
stopped waiting for kept their threads and small reads queued for minutes.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from sentinelx_core.handlers import build_registry
from sentinelx_core.handlers import fileops as F
from sentinelx_core.policy import FileOpsPath, Policy


def _policy(tmp_path: Path, **caps) -> Policy:
    p = Policy(file_ops_paths=(FileOpsPath(path=str(tmp_path), access="r"),))
    object.__setattr__(p, "upload_base", tmp_path)
    for k, v in caps.items():
        object.__setattr__(p, k, v)
    return p


def _slow_iterdir(monkeypatch, delay: float) -> None:
    real = Path.iterdir

    def slow(self):
        time.sleep(delay)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", slow)


def _tree(root: Path, dirs: int, text: str = "nothing to see here\n") -> None:
    for i in range(dirs):
        d = root / f"d{i:03d}"
        d.mkdir(parents=True)
        (d / "f.txt").write_text(text)


# --- search -------------------------------------------------------------------------------

async def test_search_stops_at_the_time_budget_with_what_it_found(tmp_path, monkeypatch):
    _tree(tmp_path / "root", 40)
    monkeypatch.setattr(F, "FILEOPS_TIME_BUDGET_SECONDS", 0.15)
    _slow_iterdir(monkeypatch, 0.02)
    handlers = build_registry(policy=_policy(tmp_path))
    started = time.monotonic()
    out = await handlers["search"]({"path": str(tmp_path / "root"), "pattern": "needle"})
    assert time.monotonic() - started < 3
    assert out["truncated"] is True and out["truncated_reason"] == "time_budget"
    assert "Narrow the path" in out["note"]
    assert out["files_searched"] < 40


async def test_one_huge_file_cannot_eat_the_whole_budget(tmp_path, monkeypatch):
    big = tmp_path / "big.txt"
    big.write_text("x\n" * 200_000)
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(F, "FILEOPS_TIME_BUDGET_SECONDS", 10.0)
    monkeypatch.setattr(F, "_BUDGET_CHECK_EVERY_LINES", 1000)
    monkeypatch.setattr(F.time, "monotonic", fake_monotonic)
    handlers = build_registry(policy=_policy(tmp_path))
    out = await handlers["search"]({"path": str(big), "pattern": "needle"})
    assert out["truncated_reason"] == "time_budget"
    assert out["files_searched"] == 1          # stopped inside the file


async def test_hitting_max_results_still_reads_as_before_with_its_reason(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n" * 50)
    handlers = build_registry(policy=_policy(tmp_path, file_ops_max_search_results=5))
    out = await handlers["search"]({"path": str(tmp_path), "pattern": "needle"})
    assert out["truncated"] is True and out["truncated_reason"] == "max_results"
    assert len(out["matches"]) == 5


async def test_a_search_that_finishes_has_no_reason(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n")
    handlers = build_registry(policy=_policy(tmp_path))
    out = await handlers["search"]({"path": str(tmp_path), "pattern": "needle"})
    assert out["truncated"] is False and "truncated_reason" not in out


# --- list -----------------------------------------------------------------------------------

async def test_recursive_list_with_a_rare_glob_stops_at_the_budget(tmp_path, monkeypatch):
    _tree(tmp_path / "root", 40)
    monkeypatch.setattr(F, "FILEOPS_TIME_BUDGET_SECONDS", 0.15)
    _slow_iterdir(monkeypatch, 0.02)
    handlers = build_registry(policy=_policy(tmp_path))
    out = await handlers["list"]({"path": str(tmp_path / "root"), "depth": 3, "glob": "*.nomatch"})
    assert out["truncated"] is True and out["truncated_reason"] == "time_budget"


# --- the pools ---------------------------------------------------------------------------------

async def test_a_read_answers_while_every_scan_thread_is_busy(tmp_path):
    small = tmp_path / "small.txt"
    small.write_text("hello\n")
    busy = [F._SCAN_POOL.submit(time.sleep, 1.5) for _ in range(F._SCAN_POOL._max_workers)]
    try:
        handlers = build_registry(policy=_policy(tmp_path))
        started = time.monotonic()
        out = await handlers["read"]({"path": str(small)})
        assert time.monotonic() - started < 0.5
        assert "hello" in str(out)
    finally:
        for f in busy:
            f.result()


# --- issue #53: the budget counts the wait for a scan worker ------------------------------

import asyncio  # noqa: E402


def _occupy_scan_pool(seconds: float):
    return [F._SCAN_POOL.submit(time.sleep, seconds) for _ in range(F._SCAN_POOL._max_workers)]


@pytest.mark.parametrize("op, payload", [
    ("search", {"pattern": "needle"}),
    ("list", {"depth": 2}),
])
async def test_a_scan_queued_past_its_budget_does_not_start(tmp_path, monkeypatch, op, payload):
    # FalconZip's reproduction: four busy workers, a fifth scan. Before, it got a
    # fresh budget once a worker freed up and finished untruncated.
    (tmp_path / "a.txt").write_text("needle\n")
    monkeypatch.setattr(F, "FILEOPS_TIME_BUDGET_SECONDS", 0.1)
    busy = _occupy_scan_pool(0.3)
    try:
        handlers = build_registry(policy=_policy(tmp_path))
        out = await handlers[op]({"path": str(tmp_path), **payload})
    finally:
        for f in busy:
            f.result()
    assert out["truncated"] is True and out["truncated_reason"] == "time_budget"
    assert out["not_started"] is True and "did not start" in out["note"]
    assert out.get("files_searched", 0) == 0 and out.get("total", 0) == 0


async def test_a_scan_that_gets_a_worker_in_time_still_runs(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("needle\n")
    monkeypatch.setattr(F, "FILEOPS_TIME_BUDGET_SECONDS", 2.0)
    busy = _occupy_scan_pool(0.2)
    try:
        handlers = build_registry(policy=_policy(tmp_path))
        out = await handlers["search"]({"path": str(tmp_path), "pattern": "needle"})
    finally:
        for f in busy:
            f.result()
    assert out["truncated"] is False and len(out["matches"]) == 1 and "not_started" not in out


async def test_a_scan_cancelled_while_queued_never_walks(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("needle\n")
    walked = []
    real = Path.iterdir

    def spy(self):
        walked.append(self)
        return real(self)

    monkeypatch.setattr(Path, "iterdir", spy)
    busy = _occupy_scan_pool(0.4)
    try:
        handlers = build_registry(policy=_policy(tmp_path))
        task = asyncio.create_task(handlers["search"]({"path": str(tmp_path), "pattern": "needle"}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        for f in busy:
            f.result()
    time.sleep(0.1)          # a worker is free now; a scan that wasn't cancelled would run
    assert walked == []
