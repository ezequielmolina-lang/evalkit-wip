"""Fileguard tests: saved sessions must be owner-only files, written atomically.

The load-bearing property: a storage_state file holds live session cookies for
the vendor's production tenant, so (a) it must never be readable by other local
accounts, and (b) a crash mid-write must never leave a truncated state that a
later run half-authenticates with.

Windows is the case that actually bites: `os.chmod(0o600)` there is a silent
no-op (it only toggles the read-only flag) and the file inherits the parent's
broad ACLs. The fix is an icacls ACL: strip inheritance, grant the current
user alone -- and on DIRECTORIES the grant must carry (OI)(CI) inherit flags,
or files created inside end up with an empty DACL and cannot be opened at all.
"""
import json
import sys
from pathlib import Path

import pytest

from lxp_eval.evalkit.fileguard import restrict_to_user, write_private_text
from lxp_eval.evalkit.session import SessionStore


class RunRecorder:
    """Stands in for subprocess.run; records each command line."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[list[str]] = []
        self.raises = raises

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if self.raises is not None:
            raise self.raises


class ChmodRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, int]] = []

    def __call__(self, path, mode):
        self.calls.append((Path(path), mode))


# --- restrict_to_user: command construction (injected, no real ACL change) ---


def test_windows_file_grant_is_current_user_only(tmp_path):
    run = RunRecorder()
    ok = restrict_to_user(
        tmp_path / "s.json",
        is_dir=False,
        run=run,
        env={"USERNAME": "eze", "USERDOMAIN": "WB"},
        platform="win32",
    )
    assert ok
    (cmd,) = run.calls
    assert cmd[0] == "icacls"
    assert "/inheritance:r" in cmd
    grant = cmd[cmd.index("/grant:r") + 1]
    assert grant == "WB\\eze:F"


def test_windows_directory_grant_is_inheritable(tmp_path):
    # Without (OI)(CI) on the directory, children get an empty DACL and even
    # the owner cannot open them -- the exact failure this module exists for.
    run = RunRecorder()
    restrict_to_user(
        tmp_path,
        is_dir=True,
        run=run,
        env={"USERNAME": "eze"},
        platform="win32",
    )
    (cmd,) = run.calls
    grant = cmd[cmd.index("/grant:r") + 1]
    assert grant == "eze:(OI)(CI)F"


def test_windows_without_username_does_nothing(tmp_path):
    run = RunRecorder()
    ok = restrict_to_user(
        tmp_path / "s.json", is_dir=False, run=run, env={}, platform="win32"
    )
    assert not ok
    assert run.calls == []


def test_windows_icacls_failure_is_best_effort(tmp_path):
    # A transient icacls failure must never block saving a session.
    run = RunRecorder(raises=OSError("icacls missing"))
    ok = restrict_to_user(
        tmp_path / "s.json",
        is_dir=False,
        run=run,
        env={"USERNAME": "eze"},
        platform="win32",
    )
    assert not ok


def test_posix_chmods_600_file_700_dir(tmp_path):
    chmod = ChmodRecorder()
    restrict_to_user(tmp_path / "s.json", is_dir=False, chmod=chmod, platform="linux")
    restrict_to_user(tmp_path, is_dir=True, chmod=chmod, platform="darwin")
    assert chmod.calls == [(tmp_path / "s.json", 0o600), (tmp_path, 0o700)]


def test_posix_chmod_failure_is_best_effort(tmp_path):
    def broken(path, mode):
        raise OSError("read-only fs")

    ok = restrict_to_user(tmp_path, is_dir=True, chmod=broken, platform="linux")
    assert not ok


# --- write_private_text: atomic + restricted -------------------------------


def test_write_private_text_writes_content_and_removes_tmp(tmp_path):
    target = tmp_path / "state.json"
    write_private_text(target, '{"ok": true}')
    assert target.read_text(encoding="utf-8") == '{"ok": true}'
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_write_private_text_replaces_existing_content(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    write_private_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_private_text_restricts_before_replace(tmp_path):
    # The ACL must land on the .tmp sibling while the content is still there,
    # BEFORE the replace -- so the cookies are never on disk under the broad
    # inherited ACLs, not even for an instant. os.replace then carries the
    # ACL onto the final name (the Windows integration test below proves the
    # end state for real).
    seen: list[tuple[str, bool, bool]] = []

    def spy(path, *, is_dir, **kwargs):
        seen.append((Path(path).name, is_dir, Path(path).exists()))
        return True

    write_private_text(tmp_path / "state.json", "x", restrict=spy)
    assert seen == [("state.json.tmp", False, True)]


# --- SessionStore integration ----------------------------------------------


def test_session_store_save_is_atomic_and_readable_back(tmp_path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path / "sessions")
    store.save({"cookies": [{"name": "sid", "value": "abc"}]})
    assert store.load() == {"cookies": [{"name": "sid", "value": "abc"}]}
    leftovers = [
        p for p in store.directory.iterdir() if p.suffix != ".json"
    ]
    assert leftovers == []


@pytest.mark.skipif(sys.platform != "win32", reason="ACLs are Windows-only")
def test_session_store_file_acl_lists_only_current_user(tmp_path):
    # Real icacls, real ACL: after save, no inherited groups (Users, SYSTEM,
    # Administrators) may remain on the storage state file.
    import os
    import subprocess

    store = SessionStore("plataforma_a", "student", directory=tmp_path / "sessions")
    store.save({"cookies": []})
    out = subprocess.run(
        ["icacls", str(store.path)], capture_output=True, text=True, check=True
    ).stdout
    assert os.environ["USERNAME"] in out
    for inherited in ("BUILTIN\\Users", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"):
        assert inherited not in out
