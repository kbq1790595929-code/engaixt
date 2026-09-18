from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
root_str = str(ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)

import core.pipeline_context as pipeline_context
import core.manifest as manifest_mod


@pytest.fixture(autouse=True)
def _isolate_runtime_state(tmp_path, monkeypatch):
    """Never let a test run overwrite the user's real diagnostics workspace."""
    monkeypatch.setenv("ENGAIXT_STATE_DIR", str(tmp_path / "app_state"))
    monkeypatch.setenv("ENGAIXT_USAGE_DB_PATH", str(tmp_path / "usage_statistics_v2.db"))
    original_get_config = pipeline_context.get_config

    def get_isolated_config():
        config = copy.deepcopy(original_get_config())
        config.workspace_dir = str(tmp_path / "pipeline_workspaces")
        return config

    monkeypatch.setattr(pipeline_context, "get_config", get_isolated_config)


@pytest.fixture(autouse=True)
def _isolate_manifest_state(tmp_path, monkeypatch):
    """Never let a test run write manifests into the user's real game library.

    GameManifest.save() writes into `core.manifest.MANIFEST_DIR`; without this
    isolation a pipeline test would leave a bogus "unknown engine" card in the
    real translated-games library (the pytest temp dir shows up as a game).
    """
    monkeypatch.setattr(manifest_mod, "BASE_DIR", tmp_path / "app_state")
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", tmp_path / "app_state" / "manifests")
    monkeypatch.setattr(manifest_mod, "BACKUP_DIR", tmp_path / "app_state" / "backups")
    monkeypatch.setattr(manifest_mod, "WORKSPACES_DIR", tmp_path / "app_state" / "workspaces")
