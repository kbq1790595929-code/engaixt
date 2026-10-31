from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.manifest as manifest_module
from core.manifest import GameManifest
from core.pipeline import Pipeline
from core.pipeline_runtime_stage import _copy_back_safe, backup_game_files
from engines.base import TextItem
from engines.tyrano import deploy as deploy_module
from engines.tyrano.deploy import deploy_tyrano_outputs


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest_module, "MANIFEST_DIR", tmp_path / "manifests")
    monkeypatch.setattr(manifest_module, "BACKUP_DIR", tmp_path / "backups")
    game = tmp_path / "game"
    workspace = tmp_path / "workspace"
    game.mkdir()
    workspace.mkdir()
    return game, workspace, GameManifest.for_game(game)


def _output(game: Path, workspace: Path, rel: str, before: bytes, after: bytes):
    target = game / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(before)
    staged = workspace / "staged" / rel
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(after)
    item = TextItem(file=rel, original="原文", translated="译文")
    return target, staged, item


def test_empty_or_unchanged_translations_do_not_require_outputs_or_backups(deployment):
    game, workspace, _manifest = deployment
    items = [TextItem(file="game.ks", original="原文", translated="原文")]
    assert deploy_tyrano_outputs(game, workspace, [], None, None) == {"files": 0}
    assert deploy_tyrano_outputs(game, workspace, items, None, None) == {"files": 0}
    assert not (game / "game.ks").exists()


def test_loose_outputs_are_deployed_with_verified_manifest_backups(deployment):
    game, workspace, manifest = deployment
    first, stage_first, item_first = _output(game, workspace, "data/scenario/1.ks", b"old 1", b"new 1")
    second, stage_second, item_second = _output(game, workspace, "data/scenario/2.ks", b"old 2", b"new 2")

    result = deploy_tyrano_outputs(
        game, workspace, [item_first, item_second],
        {item_first.file: stage_first, item_second.file: stage_second}, manifest,
    )

    assert result == {"files": 2}
    assert first.read_bytes() == b"new 1"
    assert second.read_bytes() == b"new 2"
    assert (manifest.backup_root / "data/scenario/1.ks").read_bytes() == b"old 1"
    assert (manifest.backup_root / "data/scenario/2.ks").read_bytes() == b"old 2"
    assert len(manifest.data["modified_files"]) == 2
    assert not list(game.rglob(".engaixt-tyrano-*.tmp"))


def test_embedded_output_is_deployed_via_shared_copy_back(deployment):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "Game.exe", b"MZ old", b"MZ staged")
    engine = SimpleNamespace(name="tyrano", _tyrano_staged_outputs={"Game.exe": stage}, _manifest=manifest)

    _copy_back_safe(SimpleNamespace(root=workspace), game, [item], engine)

    assert target.read_bytes() == b"MZ staged"
    assert (manifest.backup_root / "Game.exe").read_bytes() == b"MZ old"
    assert engine._tyrano_deploy_stats == {"files": 1}


@pytest.mark.parametrize("mapping_kind", ["missing", "extra", "outside_workspace"])
def test_invalid_staged_mapping_is_rejected_before_any_write(deployment, tmp_path, mapping_kind):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "data/scenario/a.ks", b"original", b"patched")
    if mapping_kind == "missing":
        mapping = None
    elif mapping_kind == "extra":
        mapping = {item.file: stage, "other.ks": stage}
    else:
        outside = tmp_path / "outside.ks"
        outside.write_bytes(b"foreign")
        mapping = {item.file: outside}
    with pytest.raises((ValueError, FileNotFoundError)):
        deploy_tyrano_outputs(game, workspace, [item], mapping, manifest)
    assert target.read_bytes() == b"original"
    assert not manifest.backup_root.exists()


@pytest.mark.parametrize("rel", ["../outside.ks", "a/../../outside.ks", "C:/escape.ks", "\\\\server\\share\\escape.ks", "/root.ks"])
def test_output_path_escape_is_rejected(deployment, rel):
    game, workspace, manifest = deployment
    staged = workspace / "stage.ks"
    staged.write_bytes(b"patched")
    item = TextItem(file=rel, original="原文", translated="译文")
    with pytest.raises(ValueError):
        deploy_tyrano_outputs(game, workspace, [item], {rel: staged}, manifest)
    assert not manifest.backup_root.exists()


def test_backup_failure_never_modifies_game(deployment, monkeypatch):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "Game.exe", b"original", b"patched")
    monkeypatch.setattr(manifest, "backup_file", lambda _target: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        deploy_tyrano_outputs(game, workspace, [item], {item.file: stage}, manifest)
    assert target.read_bytes() == b"original"
    assert not list(game.rglob(".engaixt-tyrano-*.tmp"))


def test_second_replace_failure_restores_current_state_not_historical_backups(deployment, monkeypatch):
    game, workspace, manifest = deployment
    first, stage_first, item_first = _output(game, workspace, "a.ks", b"original a", b"next a")
    second, stage_second, item_second = _output(game, workspace, "b.ks", b"original b", b"next b")
    unrelated = game / "unrelated.ks"
    unrelated.write_bytes(b"original unrelated")
    manifest.backup_file(first)
    manifest.backup_file(unrelated)
    first.write_bytes(b"existing patch a")
    unrelated.write_bytes(b"existing user data")
    real_replace = deploy_module.os.replace

    def fail_second(src, dest):
        if Path(dest) == second:
            raise OSError("second replacement failed")
        return real_replace(src, dest)

    monkeypatch.setattr(deploy_module.os, "replace", fail_second)
    with pytest.raises(OSError, match="second replacement failed"):
        deploy_tyrano_outputs(game, workspace, [item_first, item_second],
                              {item_first.file: stage_first, item_second.file: stage_second}, manifest)
    assert first.read_bytes() == b"existing patch a"
    assert second.read_bytes() == b"original b"
    assert unrelated.read_bytes() == b"existing user data"
    assert (manifest.backup_root / "a.ks").read_bytes() == b"original a"


def test_deployed_byte_mismatch_restores_snapshot(deployment, monkeypatch):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "a.ks", b"previous bytes", b"changed bytes")
    real_digest = deploy_module._digest
    injected = False

    def fail_post_replace(path):
        nonlocal injected
        if path == target and target.read_bytes() == b"changed bytes" and not injected:
            injected = True
            return 0, "not-the-staged-hash"
        return real_digest(path)

    monkeypatch.setattr(deploy_module, "_digest", fail_post_replace)
    with pytest.raises(OSError, match="deployed file verification failed"):
        deploy_tyrano_outputs(game, workspace, [item], {item.file: stage}, manifest)
    assert target.read_bytes() == b"previous bytes"


def test_manifest_record_failure_restores_snapshot(deployment, monkeypatch):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "a.ks", b"previous bytes", b"changed bytes")
    monkeypatch.setattr(manifest, "record_modified", lambda _target: (_ for _ in ()).throw(OSError("manifest failed")))
    with pytest.raises(OSError, match="manifest failed"):
        deploy_tyrano_outputs(game, workspace, [item], {item.file: stage}, manifest)
    assert target.read_bytes() == b"previous bytes"


def test_incomplete_preexisting_backup_is_rejected(deployment):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "a.ks", b"original file", b"changed")
    manifest.backup_root.mkdir(parents=True)
    (manifest.backup_root / "a.ks").write_bytes(b"partial")
    with pytest.raises(OSError, match="unrecorded backup"):
        deploy_tyrano_outputs(game, workspace, [item], {item.file: stage}, manifest)
    assert target.read_bytes() == b"original file"


def test_early_backup_stage_does_not_rewrite_missing_historical_backup(deployment, monkeypatch):
    game, workspace, manifest = deployment
    target, stage, item = _output(game, workspace, "a.ks", b"original", b"patched")
    manifest.backup_file(target)
    backup = manifest.backup_root / "a.ks"
    backup.unlink()
    target.write_bytes(b"previous patch")
    prior = copy.deepcopy(manifest.data["modified_files"])

    def unexpected_backup(_target):
        raise AssertionError("Tyrano must back up inside its transaction")

    monkeypatch.setattr(manifest, "backup_file", unexpected_backup)
    backup_game_files(SimpleNamespace(manifest=manifest), game, [item], SimpleNamespace(name="tyrano"))
    assert manifest.data["modified_files"] == prior
    assert not backup.exists()
    assert target.read_bytes() == b"previous patch"


def test_workspace_inside_game_is_supported_without_overlapping_target(deployment):
    game, _workspace, manifest = deployment
    workspace = game / "workspace"
    workspace.mkdir()
    target, stage, item = _output(game, workspace, "a.ks", b"original", b"patched")
    deploy_tyrano_outputs(game, workspace, [item], {item.file: stage}, manifest)
    assert target.read_bytes() == b"patched"


def test_staged_source_cannot_be_another_deployment_target(deployment):
    game, _workspace, manifest = deployment
    workspace = game
    first, _stage, item_first = _output(game, workspace, "a.ks", b"original", b"patched")
    with pytest.raises(ValueError, match="cannot also be a deployment target"):
        deploy_tyrano_outputs(game, workspace, [item_first], {item_first.file: first}, manifest)
    assert first.read_bytes() == b"original"


def test_repack_failure_does_not_restore_all_historical_backups(deployment, monkeypatch):
    game, workspace, manifest = deployment
    target, _stage, item = _output(game, workspace, "a.ks", b"old original", b"new")
    manifest.backup_file(target)
    target.write_bytes(b"current user changes")
    pipe = Pipeline()
    pipe.workspace = SimpleNamespace(root=workspace)
    pipe.manifest = manifest
    monkeypatch.setattr(pipe, "_fail_stage_code", lambda *args, **kwargs: False)

    class Engine:
        name = "tyrano"
        def repack(self, *_args):
            raise ValueError("pre-deployment failure")

    assert pipe._run_repack_stage(game, Engine(), [item]) is False
    assert target.read_bytes() == b"current user changes"
