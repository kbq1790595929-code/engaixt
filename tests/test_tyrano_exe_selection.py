from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from engines.tyrano import TyranoEngine
from core import manifest as manifest_module
from core.manifest import GameManifest
from core.pipeline_runtime_stage import _copy_back_safe


def _write_exe(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * max(size - 2, 0))


def _write_packed_tyrano(path: Path, size: int = 1024 * 1024) -> None:
    _write_exe(path, size)
    with ZipFile(path, "a") as archive:
        for name in (
            "index.html",
            "package.json",
            "tyrano/plugins/kag/kag.tag_system.js",
        ):
            archive.writestr(name, "placeholder")
        archive.writestr("data/scenario/start.ks", "「元の台詞です」[p]\n")


def _write_loose_tyrano(root: Path) -> Path:
    scenario = root / "data" / "scenario" / "start.ks"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("「朝の挨拶です」[p]\n", encoding="utf-8")
    kag = root / "tyrano" / "plugins" / "kag" / "kag.tag_system.js"
    kag.parent.mkdir(parents=True)
    kag.write_text("// kag", encoding="utf-8")
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    return scenario


def test_packed_tyrano_exe_is_preferred_to_larger_installer(tmp_path):
    game = tmp_path / "Game.exe"
    _write_packed_tyrano(game)
    _write_exe(tmp_path / "setup.exe", 2 * 1024 * 1024)

    assert TyranoEngine().find_exe(tmp_path) == game


def test_loose_tyrano_selects_game_not_tools_or_installer(tmp_path):
    game = tmp_path / "Game.exe"
    _write_exe(game, 100 * 1024)
    _write_exe(tmp_path / "nwjc.exe", 2 * 1024 * 1024)
    _write_exe(tmp_path / "setup.exe", 3 * 1024 * 1024)
    _write_exe(tmp_path / "updater.exe", 4 * 1024 * 1024)

    assert TyranoEngine().find_exe(tmp_path) == game


def test_selected_executable_is_preserved(tmp_path):
    packed = tmp_path / "Game.exe"
    _write_packed_tyrano(packed)
    selected = tmp_path / "Start.exe"
    _write_exe(selected, 1024)

    assert TyranoEngine().find_exe(selected) == selected


def test_selected_packed_exe_is_detected_and_patched_without_touching_other_game(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    other = game / "LargerGame.exe"
    selected = game / "SelectedGame.exe"
    _write_packed_tyrano(other, 2 * 1024 * 1024)
    _write_packed_tyrano(selected)
    original_other = other.read_bytes()
    original_selected = selected.read_bytes()
    engine = TyranoEngine()

    score, evidence = engine.detect_confidence(selected)
    items = engine.unpack(selected, tmp_path / "workspace")
    assert score == 99
    assert any(selected.name in item for item in evidence)
    assert items and all(item.meta["packed_exe_name"] == selected.name for item in items)
    assert engine.find_exe(selected) == selected

    for item in items:
        item.translated = "「原来的台词」"
    engine.repack(items, tmp_path / "workspace")
    assert selected.read_bytes() == original_selected
    assert other.read_bytes() == original_other
    monkeypatch.setattr(manifest_module, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(manifest_module, "MANIFEST_DIR", tmp_path / "manifests")
    engine._manifest = GameManifest(game)
    _copy_back_safe(SimpleNamespace(root=tmp_path / "workspace"), selected, items, engine)
    assert other.read_bytes() == original_other
    with ZipFile(selected) as archive:
        assert "原来的台词" in archive.read("data/scenario/start.ks").decode("utf-8")


def test_selected_unpacked_exe_is_not_misidentified_as_other_packed_game(tmp_path):
    other = tmp_path / "OtherGame.exe"
    selected = tmp_path / "SelectedGame.exe"
    _write_packed_tyrano(other)
    _write_exe(selected, 1024)
    original_other = other.read_bytes()
    engine = TyranoEngine()

    assert not engine.detect(selected)
    assert engine.detect_confidence(selected) == (0, [])
    with pytest.raises(ValueError, match="所选文件"):
        engine.unpack(selected, tmp_path / "workspace")
    assert other.read_bytes() == original_other


def test_selected_loose_exe_uses_loose_project_not_other_packed_game(tmp_path):
    scenario = _write_loose_tyrano(tmp_path)
    other = tmp_path / "OtherGame.exe"
    selected = tmp_path / "SelectedGame.exe"
    _write_packed_tyrano(other)
    _write_exe(selected, 1024)
    original_other = other.read_bytes()
    engine = TyranoEngine()

    score, evidence = engine.detect_confidence(selected)
    items = engine.unpack(selected, tmp_path / "workspace")
    assert score == 99
    assert any("场景目录" in item for item in evidence)
    assert [item.original for item in items] == ["「朝の挨拶です」"]
    assert all(not item.meta["packed_exe_name"] for item in items)
    assert engine.find_exe(selected) == selected
    assert scenario.read_text(encoding="utf-8") == "「朝の挨拶です」[p]\n"
    assert other.read_bytes() == original_other


def test_directory_with_loose_and_packed_uses_packed_consistently(tmp_path):
    _write_loose_tyrano(tmp_path)
    packed = tmp_path / "PackedGame.exe"
    _write_packed_tyrano(packed)
    engine = TyranoEngine()

    score, evidence = engine.detect_confidence(tmp_path)
    items = engine.unpack(tmp_path, tmp_path / "workspace")
    assert score == 99
    assert any(packed.name in item for item in evidence)
    assert items and all(item.meta["packed_exe_name"] == packed.name for item in items)
    assert engine.find_exe(tmp_path) == packed


def test_selected_package_archive_uses_external_runtime_not_another_packed_game(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    runtime = game / "Game.exe"
    _write_exe(runtime, 1024)
    _write_packed_tyrano(game / "OtherGame.exe", 2 * 1024 * 1024)
    package = game / "package.nw"
    with ZipFile(package, "w") as archive:
        for name in ("index.html", "package.json", "tyrano/plugins/kag/kag.tag_system.js"):
            archive.writestr(name, "placeholder")
        archive.writestr("data/scenario/start.ks", "「元の台詞です」[p]\n")
    engine = TyranoEngine()

    assert engine.detect(package)
    assert engine.find_exe(package) == runtime
    items = engine.unpack(package, tmp_path / "workspace")
    assert items and all(item.file == "package.nw" for item in items)


def test_selected_package_directory_finds_runtime_in_parent(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    runtime = game / "Game.exe"
    _write_exe(runtime, 1024)
    _write_packed_tyrano(game / "OtherGame.exe", 2 * 1024 * 1024)
    package = game / "package.nw"
    _write_loose_tyrano(package)
    engine = TyranoEngine()

    assert engine.detect(package)
    assert engine.find_exe(package) == runtime
    items = engine.unpack(package, tmp_path / "workspace")
    assert items and all(item.file == "data/scenario/start.ks" for item in items)


def test_nested_game_and_batch_fallback_are_preserved(tmp_path):
    nested = tmp_path / "game" / "Game.exe"
    _write_exe(nested, 1024)
    assert TyranoEngine().find_exe(tmp_path) == nested

    nested.unlink()
    start = tmp_path / "start.bat"
    start.write_text("@echo off\n", encoding="ascii")
    assert TyranoEngine().find_exe(tmp_path) == start


def test_nwjc_is_not_selected_as_game(tmp_path):
    _write_exe(tmp_path / "nwjc.exe", 1024)

    assert TyranoEngine().find_exe(tmp_path) is None
