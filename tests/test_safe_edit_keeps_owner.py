"""An edit never hands a file to the agent's user (sxrep_916B3X4TFK4K).

safe-edit wrote the new content to a temp file and renamed it over the
target. Running unprivileged, the agent can't chown that temp file to the
original owner, so the rename gave the file to the agent: a mode-660 test
file of user micha ended up owned by sentinelx and micha couldn't read it.
When the owner can't be kept, the content now goes into the existing file.

The suite runs as root, where chown always works, so the unprivileged case is
reproduced by making os.chown raise, as the agent's would.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sentinelx_core.vendored import pensa_safe_edit as PSE
from sentinelx_core.vendored.pensa_safe_edit import EditSpec, SafeEditError, apply_edit


def _no_chown(monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "chown", boom)


def _spec(f, tmp_path, **kw):
    return EditSpec(path=str(f), mode="write", new="new content\n",
                    backup_dir=str(tmp_path / "bk"), **kw)


def test_without_chown_the_edit_goes_into_the_same_file(tmp_path, monkeypatch):
    f = tmp_path / "test_rollout.py"
    f.write_text("old content\n")
    os.chmod(f, 0o660)
    before = f.stat()
    _no_chown(monkeypatch)
    res = apply_edit(_spec(f, tmp_path))
    after = f.stat()
    assert res.in_place is True and res.chown_skipped is False
    assert after.st_ino == before.st_ino            # the same file: owner and group kept
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o777 == 0o660
    assert f.read_text() == "new content\n"
    assert Path(res.backup).read_text() == "old content\n"   # backup taken first


def test_when_chown_works_the_edit_stays_atomic(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("old content\n")
    inode = f.stat().st_ino
    res = apply_edit(_spec(f, tmp_path))
    assert res.in_place is False and f.stat().st_ino != inode   # renamed into place
    assert f.read_text() == "new content\n"


def test_without_posix_owners_the_rename_is_kept(tmp_path, monkeypatch):
    # Windows: copy_metadata always reports the skip; there is no owner to lose.
    f = tmp_path / "f.txt"
    f.write_text("old content\n")
    inode = f.stat().st_ino
    monkeypatch.delattr(os, "chown")
    res = apply_edit(_spec(f, tmp_path))
    assert res.in_place is False and f.stat().st_ino != inode


def test_a_file_the_agent_cannot_write_is_left_alone(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("old content\n")
    _no_chown(monkeypatch)
    real_open = open

    def guarded_open(path, mode="r", *a, **k):
        if Path(path) == f and "+" in mode:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(PSE, "open", guarded_open, raising=False)
    with pytest.raises(SafeEditError) as e:
        apply_edit(_spec(f, tmp_path))
    assert e.value.code == "not_writable"
    assert f.read_text() == "old content\n"
    assert not list(tmp_path.glob("f.txt.*"))       # no temp left beside it


def test_restoring_a_backup_keeps_the_file_too(tmp_path, monkeypatch):
    f = tmp_path / "f.txt"
    f.write_text("old content\n")
    res = apply_edit(_spec(f, tmp_path))
    backup = res.backup
    inode = f.stat().st_ino
    _no_chown(monkeypatch)
    out = apply_edit(EditSpec(path=str(f), mode=None, restore=backup,
                              backup_dir=str(tmp_path / "bk")))
    assert out.in_place is True and f.stat().st_ino == inode
    assert f.read_text() == "old content\n"


def test_the_model_is_told_it_was_written_in_place(capsys):
    PSE._render_result(PSE.EditResult(ok=True, action="edit", target="/x", changed=1,
                                      backup="/b", in_place=True))
    out = capsys.readouterr().out
    assert "written in place to keep the file's owner" in out and "chown_skipped" not in out
