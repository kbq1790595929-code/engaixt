from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

from core.manifest import GameManifest
from core.pipeline_runtime_stage import _copy_back_safe
from engines.tyrano import TyranoEngine


def _project_files() -> dict[str, str]:
    return {
        "index.html": "<html></html>",
        "package.json": '{"name":"tyranoscript"}',
        "tyrano/plugins/kag/kag.tag_system.js": "// kag",
        "data/scenario/scene.ks": "今日は晴れですね[p]\n",
        "data/bgimage/keep.txt": "unchanged asset",
    }


def _game(tmp_path: Path, *, zipped: bool) -> tuple[Path, Path]:
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "Game.exe"
    exe.write_bytes(b"MZ")
    package = game / "package.nw"
    if zipped:
        with ZipFile(package, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in _project_files().items():
                archive.writestr(name, content)
    else:
        for name, content in _project_files().items():
            path = package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    return game, exe


def test_detect_and_patch_loose_package_nw_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("core.manifest.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("core.manifest.MANIFEST_DIR", tmp_path / "manifests")
    game, exe = _game(tmp_path, zipped=False)
    workspace = tmp_path / "workspace"
    engine = TyranoEngine()
    engine._manifest = GameManifest(game)

    assert engine.detect_confidence(game)[0] == 99
    assert engine.detect_confidence(exe)[0] == 99
    items = engine.unpack(exe, workspace)
    assert [item.original for item in items] == ["今日は晴れですね"]
    assert items[0].file == "package.nw/data/scenario/scene.ks"
    items[0].translated = "今天天气真好"
    engine.repack(items, workspace)
    _copy_back_safe(SimpleNamespace(root=workspace), exe, items, engine)

    assert (game / "package.nw/data/scenario/scene.ks").read_text(encoding="utf-8") == "今天天气真好[p]\n"
    assert engine.verify_repack(game, items, {})["hits"] == 1
    assert (game / "package.nw/data/bgimage/keep.txt").read_text(encoding="utf-8") == "unchanged asset"


def test_detect_and_patch_package_nw_zip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.manifest.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("core.manifest.MANIFEST_DIR", tmp_path / "manifests")
    game, exe = _game(tmp_path, zipped=True)
    before = (game / "package.nw").read_bytes()
    workspace = tmp_path / "workspace"
    engine = TyranoEngine()
    engine._manifest = GameManifest(game)

    assert engine.detect_confidence(game)[0] == 99
    assert engine.detect_confidence(exe)[0] == 99
    items = engine.unpack(exe, workspace)
    assert [item.original for item in items] == ["今日は晴れですね"]
    assert items[0].file == "package.nw"
    items[0].translated = "今天天气真好"
    engine.repack(items, workspace)
    assert (game / "package.nw").read_bytes() == before
    _copy_back_safe(SimpleNamespace(root=workspace), exe, items, engine)

    with ZipFile(game / "package.nw") as archive:
        assert archive.read("data/scenario/scene.ks").decode("utf-8") == "今天天气真好[p]\n"
        assert archive.read("data/bgimage/keep.txt") == b"unchanged asset"
    assert engine.verify_repack(game, items, {})["hits"] == 1
