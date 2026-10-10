from __future__ import annotations

from pathlib import Path

import core.workspace as workspace_module
from core.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(base_dir=tmp_path / "workspaces")


def test_cleanup_retries_transient_windows_lock(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    attempts = []
    real_rmtree = workspace_module.shutil.rmtree

    def flaky_rmtree(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError(32, "另一个程序正在使用此文件")
        real_rmtree(path)

    monkeypatch.setattr(workspace_module.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(workspace_module.time, "sleep", lambda _seconds: None)

    assert workspace.cleanup() is True
    assert len(attempts) == 2
    assert not workspace.root.exists()


def test_cleanup_keeps_locked_workspace_and_returns_false(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(
        workspace_module.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(PermissionError(32, "文件被占用")),
    )
    monkeypatch.setattr(workspace_module.time, "sleep", lambda _seconds: None)

    assert workspace.cleanup() is False
    assert workspace.root.exists()
