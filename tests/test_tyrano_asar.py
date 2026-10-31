from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.manifest import GameManifest
from core.pipeline_runtime_stage import _copy_back_safe
from engines.tyrano import TyranoEngine
from engines.tyrano.archive import (
    _encode_asar_header,
    _read_asar_index,
    is_tyrano_archive,
    read_archive_entries,
    scenario_entry_names,
)


def _write_asar(path: Path, files: dict[str, bytes]) -> None:
    header = {"files": {}}
    payload = bytearray()
    for name, content in files.items():
        node = header["files"]
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {}).setdefault("files", {})
        digest = hashlib.sha256(content).hexdigest()
        entry = {
            "size": len(content),
            "offset": str(len(payload)),
            "integrity": {
                "algorithm": "SHA256",
                "hash": digest,
                "blockSize": 4 * 1024 * 1024,
                "blocks": [digest] if content else [],
            },
        }
        node[parts[-1]] = entry
        payload.extend(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_encode_asar_header(header) + payload)

def _project_files() -> dict[str, bytes]:
    return {
        "index.html": b"<html></html>",
        "package.json": b'{"name":"tyranoscript"}',
        "tyrano/plugins/kag/kag.tag_system.js": b"// kag",
        "data/scenario/scene.ks": "今日は晴れですね[p]\n".encode("utf-8"),
        "data/others/JKG-M_3.ttf": b"original medium font",
        "data/others/JKG-L_3.ttf": b"original light font",
        "data/bgimage/keep.txt": b"untouched asset",
    }


def test_electron_asar_detection_extract_and_transactional_patch(tmp_path, monkeypatch):
    monkeypatch.setattr("core.manifest.BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr("core.manifest.MANIFEST_DIR", tmp_path / "manifests")
    game = tmp_path / "Ikenai_Life"
    archive = game / "resources" / "app.asar"
    _write_asar(archive, _project_files())
    (game / "Ikenai_Life.exe").write_bytes(b"MZ")
    original_archive = archive.read_bytes()

    engine = TyranoEngine()
    engine._manifest = GameManifest(game)
    score, evidence = engine.detect_confidence(game)
    items = engine.unpack(game, tmp_path / "workspace")

    assert score == 99
    assert any("app.asar" in item for item in evidence)
    assert len(scenario_entry_names(archive)) == 1
    assert [item.original for item in items] == ["今日は晴れですね"]
    assert items[0].file == "resources/app.asar"
    assert items[0].meta["packed_archive_name"] == "resources/app.asar"
    items[0].translated = "今天天气真好"

    workspace = tmp_path / "workspace"
    engine.repack(items, workspace)

    staged = workspace / "tyrano_staged" / "resources" / "app.asar"
    staged_entries = read_archive_entries(staged, [
        "data/scenario/scene.ks",
        "data/bgimage/keep.txt",
    ])
    translated_script = staged_entries["data/scenario/scene.ks"]
    assert archive.read_bytes() == original_archive
    assert "今日は晴れですね".encode("utf-8") in (
        workspace / "original" / "__tyrano_embedded__" / "data" / "scenario" / "scene.ks"
    ).read_bytes()
    assert "今天天气真好".encode("utf-8") in translated_script
    assert staged_entries["data/bgimage/keep.txt"] == b"untouched asset"

    integrity = _read_asar_index(staged).entries["data/scenario/scene.ks"][1]["integrity"]
    assert integrity["hash"] == hashlib.sha256(translated_script).hexdigest()
    assert integrity["blocks"] == [hashlib.sha256(translated_script).hexdigest()]

    _copy_back_safe(SimpleNamespace(root=workspace), game, items, engine)
    result = engine.verify_repack(game, items, {})

    assert result["checked"] is True
    assert result["hits"] == 1
    assert result["archive_verified"] is True
    assert archive.read_bytes() != original_archive
    assert read_archive_entries(archive, ["data/bgimage/keep.txt"])[
        "data/bgimage/keep.txt"
    ] == b"untouched asset"


def test_embedded_tyrano_repack_replaces_japanese_font_for_chinese(tmp_path, monkeypatch):
    game = tmp_path / "game"
    archive = game / "resources" / "app.asar"
    _write_asar(archive, _project_files())
    source_font = tmp_path / "SourceHanSansCN-Regular.otf"
    source_font.write_bytes(b"open source cjk font")
    monkeypatch.setattr("core.open_source_fonts.find_source_han_sans", lambda: source_font)

    engine = TyranoEngine()
    engine._target_lang = "zh-CN"
    items = engine.unpack(game, tmp_path / "workspace")
    items[0].translated = "今天天气真好"
    workspace = tmp_path / "workspace"
    engine.repack(items, workspace)

    staged = workspace / "tyrano_staged" / "resources" / "app.asar"
    entries = read_archive_entries(staged, [
        "data/others/JKG-M_3.ttf",
        "data/others/JKG-L_3.ttf",
    ])
    assert entries["data/others/JKG-M_3.ttf"] == source_font.read_bytes()
    assert entries["data/others/JKG-L_3.ttf"] == source_font.read_bytes()
    assert engine._tyrano_repack_stats["font_replacements"] == 2


def test_asar_detection_rejects_unsafe_entry_names(tmp_path):
    archive = tmp_path / "app.asar"
    _write_asar(archive, {
        "index.html": b"<html></html>",
        "package.json": b"{}",
        "tyrano/plugins/kag/kag.tag_system.js": b"// kag",
        "data/scenario/../../outside.ks": b"unsafe",
    })

    assert is_tyrano_archive(archive) is False
    with pytest.raises(ValueError, match="Unsafe Electron ASAR entry"):
        _read_asar_index(archive)
