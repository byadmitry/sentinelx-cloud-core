"""The local audit keeps what was done, not the bytes moved (sxrep_1G8KEZHMGKBA).

upload_chunk stored each ~1.4 MB base64 chunk in full; retention counts lines,
so one host's log reached ~1.7 GB in an hour.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from sentinelx_core import local_audit


@pytest.fixture(autouse=True)
def _audit_in_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(local_audit, "AUDIT_PATH", tmp_path / "audit.jsonl")


def _last_entry():
    return json.loads(local_audit.AUDIT_PATH.read_text().splitlines()[-1])


def test_an_upload_chunk_keeps_size_and_hash_not_bytes():
    raw = os.urandom(1_048_576)
    payload = {"upload_id": "u1", "index": 3, "content_base64": base64.b64encode(raw).decode()}
    local_audit.record("upload_chunk", payload, ok=True)
    line = local_audit.AUDIT_PATH.read_text().splitlines()[-1]
    assert len(line) < 1024
    summary = json.loads(line)["payload"]["content_base64"]
    assert summary == {"omitted": "base64", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    assert _last_entry()["payload"]["index"] == 3


def test_a_command_is_kept_whole():
    payload = {"command": "systemctl restart nginx", "timeout": 30}
    local_audit.record("exec", payload, ok=True)
    assert _last_entry()["payload"] == payload


def test_a_huge_text_field_is_summarised_with_its_head():
    big = "x" * (local_audit.MAX_FIELD_CHARS + 10)
    local_audit.record("edit", {"path": "/f", "new": big}, ok=True)
    s = _last_entry()["payload"]["new"]
    assert s["omitted"] == "text" and s["chars"] == len(big)
    assert s["sha256"] == hashlib.sha256(big.encode()).hexdigest() and s["head"] == big[:2048]


def test_a_script_under_the_cap_is_kept_whole():
    script = "echo hi\n" * 5000          # ~40 KB
    local_audit.record("script_run", {"content": script}, ok=True)
    assert _last_entry()["payload"]["content"] == script


def test_the_original_payload_is_not_touched():
    payload = {"content_base64": base64.b64encode(b"abc").decode(), "nested": [{"content_base64": "YQ=="}]}
    before = json.dumps(payload, sort_keys=True)
    local_audit.record("upload_file", payload, ok=True)
    assert json.dumps(payload, sort_keys=True) == before
    assert _last_entry()["payload"]["nested"][0]["content_base64"]["bytes"] == 1
