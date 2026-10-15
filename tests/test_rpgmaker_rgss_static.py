from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import struct
import zlib

import pytest

from engines.rpgmaker import RPGMakerEngine
from utils.rvdata2 import extract_strings_from_marshal, replace_strings_in_marshal_full
from utils.rgss_scripts import extract_rgss_script_literals
from utils.rgss_events import extract_rgss_event_strings


SOURCE = "\u3053\u3093\u306b\u3061\u306f\u3001\u4e16\u754c\u3002"
TARGET = "\u4f60\u597d\uff0c\u8fd9\u662f\u4e00\u6761\u6bd4\u539f\u6587\u66f4\u957f\u7684\u4e2d\u6587\u8bd1\u6587\u3002"


def _marshal(text: str = SOURCE) -> bytes:
    raw = text.encode("utf-8")
    assert len(raw) < 123
    return b'\x04\x08[\x06"' + bytes([len(raw) + 5]) + raw


def _marshal_long(value: int) -> bytes:
    if 0 <= value < 123:
        return bytes([value + 5])
    for size in range(1, 5):
        if value < (1 << (size * 8)):
            return bytes([size]) + value.to_bytes(size, "little")
    raise ValueError(value)


def _marshal_script(source: str = 'puts "' + SOURCE + '"\n# コメント\nrequire "Data/Map001.rvdata2"\n') -> bytes:
    name = b"Main"
    compressed = zlib.compress(source.encode("utf-8"))
    return (
        b"\x04\x08[" + _marshal_long(1)
        + b"[" + _marshal_long(3)
        + b"i\x06"
        + b'"' + _marshal_long(len(name)) + name
        + b'"' + _marshal_long(len(compressed)) + compressed
    )


def _game(tmp_path: Path, suffix: str) -> tuple[Path, Path, Path]:
    game = tmp_path / "game"
    (game / "Data").mkdir(parents=True)
    (game / "Game.ini").write_text("[Game]\nTitle=Regression\n", encoding="ascii")
    source = game / "Data" / f"Actors{suffix}"
    source.write_bytes(_marshal())
    untouched = game / "Data" / f"Map001{suffix}"
    untouched.write_bytes(_marshal("\u3055\u3088\u3046\u306a\u3089\u3002"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return game, source, workspace


@pytest.fixture(autouse=True)
def _no_runtime_scan(monkeypatch):
    def fail_scan(*args, **kwargs):
        pytest.fail("RGSS static extraction must never start MV/MZ runtime scanning")

    monkeypatch.setattr("core.rpgmaker_runtime.scan", fail_scan)


@pytest.mark.parametrize("suffix", [".rxdata", ".rvdata", ".rvdata2"])
def test_loose_rgss_uses_static_route(tmp_path, suffix):
    game, source, workspace = _game(tmp_path, suffix)
    engine = RPGMakerEngine()

    assert engine.detect(game)
    items = engine.unpack(game, workspace)

    assert SOURCE in {item.original for item in items}
    assert not engine._is_mv_mz
    assert source.read_bytes() == _marshal()


@pytest.mark.parametrize("suffix", [".rxdata", ".rvdata", ".rvdata2"])
def test_loose_rgss_translation_reaches_game_and_preserves_other_files(tmp_path, suffix):
    game, source, workspace = _game(tmp_path, suffix)
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    for item in items:
        if item.original == SOURCE:
            item.translated = TARGET
    untouched = game / "Data" / f"Map001{suffix}"
    original_untouched = untouched.read_bytes()

    engine.repack(items, workspace)

    assert extract_strings_from_marshal(source.read_bytes()) == [TARGET]
    assert untouched.read_bytes() == original_untouched


@pytest.mark.parametrize("suffix", [".rxdata", ".rvdata", ".rvdata2"])
def test_rgss_checkpoint_repack_works_with_fresh_engine(tmp_path, suffix):
    game, source, workspace = _game(tmp_path, suffix)
    items = RPGMakerEngine().unpack(game, workspace)
    for item in items:
        if item.original == SOURCE:
            item.translated = TARGET
    resumed = RPGMakerEngine()
    resumed._game_dir = game

    resumed.repack(items, workspace)

    assert extract_strings_from_marshal(source.read_bytes()) == [TARGET]


@pytest.mark.parametrize("suffix", [".rxdata", ".rvdata", ".rvdata2"])
def test_rgss_uninstall_restores_original_bytes(monkeypatch, tmp_path, suffix):
    import core.manifest as manifest_mod
    from core.pipeline_runtime_stage import record_modified_outputs

    state = tmp_path / "state"
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", state / "manifests")
    monkeypatch.setattr(manifest_mod, "BACKUP_DIR", state / "backups")
    monkeypatch.setattr(
        manifest_mod, "_clear_translation_cache_for_game",
        lambda game_dir: {"deleted_files": 0, "bytes_freed": 0, "errors": 0},
    )
    game, source, workspace = _game(tmp_path, suffix)
    engine = RPGMakerEngine()
    engine._manifest = manifest_mod.GameManifest.for_game(game)
    items = engine.unpack(game, workspace)
    for item in items:
        if item.original == SOURCE:
            item.translated = TARGET

    engine.repack(items, workspace)
    record_modified_outputs(SimpleNamespace(manifest=engine._manifest), game, items, engine)

    assert source.read_bytes() != _marshal()
    result = engine._manifest.restore_and_uninstall()
    assert result["errors"] == 0
    assert result["missing_backups"] == 0
    assert source.read_bytes() == _marshal()


def test_rgss_engine_state_is_reset_when_next_game_is_mv(monkeypatch, tmp_path):
    game, _, workspace = _game(tmp_path, ".rxdata")
    engine = RPGMakerEngine()
    engine.unpack(game, workspace)
    mv = tmp_path / "mv"
    (mv / "data").mkdir(parents=True)
    (mv / "data" / "Actors.json").write_text("[]", encoding="ascii")
    (mv / "nw.dll").write_bytes(b"")
    monkeypatch.setattr("core.rpgmaker_runtime.scan", lambda *a, **kw: [])

    engine.unpack(mv, tmp_path / "mv-workspace")

    assert engine._is_mv_mz
    assert not engine._is_xp_mkxp
    assert not engine._is_vxace


def test_po_localization_repack_handles_multiple_catalogs(tmp_path):
    game, source, workspace = _game(tmp_path, ".rxdata")
    languages = game / "Languages"
    (languages / "internal").mkdir(parents=True)
    for po in (languages / "ja.po", languages / "internal" / "ja.po"):
        po.write_text(f'msgid "greeting"\nmsgstr "{SOURCE}"\n', encoding="utf-8")
    engine = RPGMakerEngine()
    engine._game_dir = game
    items = engine.unpack(game, workspace)
    for item in items:
        item.translated = TARGET

    engine.repack(items, workspace)

    assert len(items) == 2
    assert all(TARGET in po.read_text(encoding="utf-8") for po in languages.rglob("*.po"))
    assert source.read_bytes() == _marshal()


def _reference_v1(entries: list[tuple[bytes, bytes]]) -> bytes:
    """Independent fixture encoder for the published RGSSAD v1 entry stream."""
    output = bytearray(b"RGSSAD\0\x01")
    key = 0xDEADCAFE
    for name, data in entries:
        output.extend(struct.pack("<I", len(name) ^ key))
        key = (key * 7 + 3) & 0xFFFFFFFF
        for value in name:
            output.append(value ^ (key & 0xFF))
            key = (key * 7 + 3) & 0xFFFFFFFF
        output.extend(struct.pack("<I", len(data) ^ key))
        key = (key * 7 + 3) & 0xFFFFFFFF
        content_key = key
        for offset, value in enumerate(data):
            if offset and offset % 4 == 0:
                content_key = (content_key * 7 + 3) & 0xFFFFFFFF
            output.append(value ^ ((content_key >> (8 * (offset % 4))) & 0xFF))
    return bytes(output)


def _archived_game(tmp_path, archive_suffix, data_suffix):
    from utils.rgss3a import pack_rgss3a

    game = tmp_path / "archived"
    game.mkdir()
    (game / "Game.ini").write_text("[Game]\n", encoding="ascii")
    archive = game / f"Game{archive_suffix}"
    resource = "Graphics/\u5c11\u5973.png"
    if archive_suffix == ".rgss3a":
        original = tmp_path / "archive-input"
        (original / "Data").mkdir(parents=True)
        (original / "Graphics").mkdir()
        (original / "Data" / f"Actors{data_suffix}").write_bytes(_marshal())
        (original / resource).write_bytes(b"image bytes must not change")
        pack_rgss3a(original, archive, base_key=17)
    else:
        archive.write_bytes(_reference_v1([
            (f"Data\\Actors{data_suffix}".encode("ascii"), _marshal()),
            (resource.replace("/", "\\").encode("cp932"), b"image bytes must not change"),
        ]))
    return game, archive


@pytest.mark.parametrize("archive_suffix,data_suffix", [
    (".rgssad", ".rxdata"), (".rgss2a", ".rvdata"), (".rgss3a", ".rvdata2"),
])
def test_rgss_archive_roundtrip_keeps_resources_names_order_and_version(
    tmp_path, archive_suffix, data_suffix,
):
    from utils.rgss_archive import read_rgss_archive

    game, archive = _archived_game(tmp_path, archive_suffix, data_suffix)
    before = read_rgss_archive(archive.read_bytes())
    engine = RPGMakerEngine()
    items = engine.unpack(game, tmp_path / "workspace")
    items[0].translated = TARGET

    engine.repack(items, tmp_path / "workspace")
    after = read_rgss_archive(archive.read_bytes())

    assert len(items) == 1
    assert after.version == before.version
    assert after.base_key == before.base_key
    assert [entry.key for entry in after.entries] == [entry.key for entry in before.entries]
    assert [entry.raw_name for entry in after.entries] == [entry.raw_name for entry in before.entries]
    data_entry = next(entry for entry in after.entries if entry.name.startswith("Data/"))
    assert extract_strings_from_marshal(data_entry.data) == [TARGET]
    assert next(entry.data for entry in after.entries if entry.name.startswith("Graphics/")) == (
        b"image bytes must not change"
    )
    assert engine.diagnostics_snapshot()["repack_verification"]["archive_roundtrip_verified"]


@pytest.mark.parametrize("archive_name", ["Game.rgss2a", "Game.rgss3a"])
def test_public_rgss_archive_fixtures_parse_and_include_scripts(tmp_path, archive_name):
    fixture = (
        Path(__file__).parent.parent
        / ".codex_runs" / "mtool-audit" / "rgss-public-fixtures" / archive_name
    )
    if not fixture.is_file():
        pytest.skip("public RGSS fixture is only present in the local audit workspace")
    game = tmp_path / archive_name.removesuffix(Path(archive_name).suffix)
    game.mkdir()
    (game / archive_name).write_bytes(fixture.read_bytes())
    (game / "Game.ini").write_text("[Game]\n", encoding="ascii")

    items = RPGMakerEngine().unpack(game, tmp_path / "workspace")

    assert items
    assert any("Scripts." in item.file for item in items)
    assert all(item.meta["source_kind"] == "rgss_marshal_script"
               for item in items if "Scripts." in item.file)


@pytest.mark.parametrize("archive_name", ["Game.rgss2a", "Game.rgss3a"])
def test_public_rgss_archive_fixtures_extract_event_command_text(tmp_path, archive_name):
    fixture = (
        Path(__file__).parent.parent
        / ".codex_runs" / "mtool-audit" / "rgss-public-fixtures" / archive_name
    )
    if not fixture.is_file():
        pytest.skip("public RGSS fixture is only present in the local audit workspace")
    archive = __import__("utils.rgss_archive", fromlist=["read_rgss_archive"]).read_rgss_archive(
        fixture.read_bytes()
    )
    map_entry = next(entry for entry in archive.entries if entry.name.casefold().endswith("map001." + ("rvdata" if archive_name.endswith("2a") else "rvdata2")))

    items = extract_rgss_event_strings(map_entry.data)

    assert items
    assert {item.event_code for item in items} == {401}
    assert {item.original for item in items} >= {"Text 1", "Text 2"}
    assert all(item.context == "event.message" for item in items)


@pytest.mark.parametrize("archive_name", ["Game.rgss2a", "Game.rgss3a"])
def test_public_rgss_archive_event_translation_reaches_archive(tmp_path, archive_name):
    fixture = (
        Path(__file__).parent.parent
        / ".codex_runs" / "mtool-audit" / "rgss-public-fixtures" / archive_name
    )
    if not fixture.is_file():
        pytest.skip("public RGSS fixture is only present in the local audit workspace")
    game = tmp_path / archive_name.removesuffix(Path(archive_name).suffix)
    game.mkdir()
    (game / archive_name).write_bytes(fixture.read_bytes())
    (game / "Game.ini").write_text("[Game]\n", encoding="ascii")
    workspace = tmp_path / "workspace"
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    event_item = next(
        item for item in items
        if item.file.casefold().endswith("map001.rvdata" if archive_name.endswith("2a") else "map001.rvdata2")
        and item.original == "Text 1"
    )
    event_item.translated = "翻译后的事件文本"

    engine.repack(items, workspace)

    archive = __import__("utils.rgss_archive", fromlist=["read_rgss_archive"]).read_rgss_archive(
        (game / archive_name).read_bytes()
    )
    data_entry = next(entry for entry in archive.entries if entry.name.casefold().endswith(
        "map001." + ("rvdata" if archive_name.endswith("2a") else "rvdata2")
    ))
    assert "翻译后的事件文本" in extract_strings_from_marshal(data_entry.data)


@pytest.mark.parametrize("archive_suffix,data_suffix", [
    (".rgssad", ".rxdata"), (".rgss2a", ".rvdata"), (".rgss3a", ".rvdata2"),
])
def test_rgss_archive_checkpoint_and_manifest_restore(
    monkeypatch, tmp_path, archive_suffix, data_suffix,
):
    import core.manifest as manifest_mod

    state = tmp_path / "state"
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", state / "manifests")
    monkeypatch.setattr(manifest_mod, "BACKUP_DIR", state / "backups")
    game, archive = _archived_game(tmp_path, archive_suffix, data_suffix)
    original_bytes = archive.read_bytes()
    workspace = tmp_path / "workspace"
    items = RPGMakerEngine().unpack(game, workspace)
    items[0].translated = TARGET
    engine = RPGMakerEngine()
    engine._game_dir = game
    engine._manifest = manifest_mod.GameManifest.for_game(game)

    engine.repack(items, workspace)

    assert archive.read_bytes() != original_bytes
    records = engine._manifest.data["modified_files"]
    assert any(record["rel"] == archive.name and record.get("backup") for record in records)
    result = engine._manifest.restore_and_uninstall()
    assert result["errors"] == result["missing_backups"] == 0
    assert archive.read_bytes() == original_bytes


@pytest.mark.parametrize("name", [b"..\\outside.rxdata", b"C:\\outside.rxdata", b"Data\\a:bad.rxdata"])
def test_rgss_archive_rejects_unsafe_paths_before_extracting(tmp_path, name):
    from utils.rgss_archive import read_rgss_archive

    with pytest.raises(ValueError, match="Unsafe"):
        read_rgss_archive(_reference_v1([(name, _marshal())]))


def test_rgss_archive_rejects_duplicate_case_aliases():
    from utils.rgss_archive import read_rgss_archive

    with pytest.raises(ValueError, match="Duplicate"):
        read_rgss_archive(_reference_v1([
            (b"Data\\Actors.rxdata", _marshal()), (b"data\\actors.rxdata", _marshal()),
        ]))


@pytest.mark.parametrize("data", [b"", b"RGSSAD\0", b"RGSSAD\0\x02", b"RGSSAD\0\x01\x01"])
def test_rgss_archive_rejects_invalid_and_truncated_inputs(data):
    from utils.rgss_archive import read_rgss_archive

    with pytest.raises(ValueError):
        read_rgss_archive(data)


def test_rgss_repack_stops_if_game_changed_after_extraction(tmp_path):
    game, source, workspace = _game(tmp_path, ".rvdata2")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    for item in items:
        item.translated = TARGET
    changed = _marshal("\u30b2\u30fc\u30e0\u66f4\u65b0\u5f8c\u306e\u6587\u7ae0\u3002")
    source.write_bytes(changed)

    with pytest.raises(ValueError, match="changed"):
        engine.repack(items, workspace)
    assert source.read_bytes() == changed
    assert not list(game.rglob("*.bak"))


def test_rgss_repack_does_not_modify_game_if_backup_fails(tmp_path):
    game, source, workspace = _game(tmp_path, ".rxdata")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    items[0].translated = TARGET

    def fail_backup(target):
        raise PermissionError("backup denied")

    engine._manifest = SimpleNamespace(backup_file=fail_backup)
    with pytest.raises(PermissionError, match="backup denied"):
        engine.repack(items, workspace)
    assert source.read_bytes() == _marshal()
    assert not list(game.rglob(".engaixt-rgss-*"))


def test_rgss_includes_script_marshal_texts(tmp_path):
    game, source, workspace = _game(tmp_path, ".rvdata2")
    script = game / "Data" / "Scripts.rvdata2"
    script.write_bytes(_marshal_script())

    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)

    script_items = [item for item in items if item.file.endswith("Scripts.rvdata2")]
    assert len(script_items) == 1
    assert script_items[0].meta["source_kind"] == "rgss_marshal_script"
    assert script_items[0].meta["script_name"] == "Main"
    assert script_items[0].meta["script_index"] == 0
    assert script_items[0].original == SOURCE
    assert not any("Data/Map001" in item.original for item in script_items)

    script_items[0].translated = TARGET
    engine.repack(items, workspace)
    literals = extract_rgss_script_literals(script.read_bytes())
    assert [literal["source"] for literal in literals] == [TARGET]
    assert source.exists()


@pytest.mark.parametrize("integer", [
    b"\x01\xff", b"\x02\x00\x01", b"\x03\x00\x00\x01", b"\x04\x00\x00\x00\x01",
    b"\xff\x80", b"\xfe\x00\xff", b"\xfd\x00\x00\xff", b"\xfc\x00\x00\x00\xff",
])
def test_marshal_integer_payload_does_not_corrupt_following_strings(integer):
    string = _marshal()[4:]
    data = b"\x04\x08[\x07i" + integer + string

    assert extract_strings_from_marshal(data) == [SOURCE]
    assert replace_strings_in_marshal_full(data, {}) == data
    patched = replace_strings_in_marshal_full(data, {SOURCE: TARGET})
    assert extract_strings_from_marshal(patched) == [TARGET]
    assert patched.startswith(b"\x04\x08[\x07i" + integer)


def test_marshal_ivar_wrapper_preserves_nested_values():
    data = b"\x04\x08I" + _marshal()[2:] + b"\0"

    assert extract_strings_from_marshal(data) == [SOURCE]
    assert replace_strings_in_marshal_full(data, {}) == data


def test_marshal_repack_rejects_trailing_or_truncated_data():
    for data in (_marshal() + b"trailing", _marshal()[:-1]):
        with pytest.raises(ValueError):
            replace_strings_in_marshal_full(data, {SOURCE: TARGET})


def test_rgss_mixed_checkpoint_routes_fail_without_writing(tmp_path):
    from engines.base import TextItem

    game, source, workspace = _game(tmp_path, ".rxdata")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    items[0].translated = TARGET
    items.append(TextItem(
        file="Languages/ja.po", original=SOURCE, translated=TARGET,
        meta={"source_kind": "po_localization"},
    ))

    with pytest.raises(ValueError, match="mixes"):
        engine.repack(items, workspace)
    assert source.read_bytes() == _marshal()


def test_rgss_diagnostics_snapshot_contains_final_verification(tmp_path):
    game, _, workspace = _game(tmp_path, ".rvdata2")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    snapshot = engine.diagnostics_snapshot()
    items[0].translated = TARGET

    engine.repack(items, workspace)

    assert snapshot["repack_verification"]["checked"]
    assert snapshot["repack_verification"]["hits"] == 1


def test_rgss_resource_filename_fields_remain_unchanged(tmp_path):
    game, source, workspace = _game(tmp_path, ".rvdata2")
    # RPG object with a resource filename and a visible message sharing the text.
    symbol = lambda text: b":" + bytes([len(text) + 5]) + text
    string = _marshal()[4:]
    data = (
        b"\x04\x08o" + symbol(b"RPG::Actor") + b"\x07"
        + symbol(b"@character_name") + string
        + symbol(b"@name") + string
    )
    source.write_bytes(data)
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    for item in items:
        if item.original == SOURCE:
            item.translated = TARGET

    engine.repack(items, workspace)

    patched = source.read_bytes()
    assert SOURCE.encode("utf-8") in patched
    assert TARGET.encode("utf-8") in patched
    assert replace_strings_in_marshal_full(patched, {}) == patched


def test_rgss_prevalidates_all_files_before_deployment(tmp_path):
    game, source, workspace = _game(tmp_path, ".rvdata2")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    for item in items:
        item.translated = TARGET
    (workspace / "original" / "Data" / "Map001.rvdata2").write_bytes(b"broken")

    with pytest.raises(ValueError):
        engine.repack(items, workspace)
    assert source.read_bytes() == _marshal()


def test_rgss_v3_rejects_payload_overlapping_entry_table(tmp_path):
    from utils.rgss_archive import read_rgss_archive

    _, archive = _archived_game(tmp_path, ".rgss3a", ".rvdata2")
    data = bytearray(archive.read_bytes())
    base = struct.unpack_from("<I", data, 8)[0]
    key = (base * 9 + 3) & 0xFFFFFFFF
    struct.pack_into("<I", data, 12, 12 ^ key)

    with pytest.raises(ValueError, match="overlapping"):
        read_rgss_archive(bytes(data))


def test_rgss_pipeline_restores_files_if_later_deployment_fails(monkeypatch, tmp_path):
    import os
    import core.manifest as manifest_mod
    from core.pipeline import Pipeline

    state = tmp_path / "state"
    monkeypatch.setattr(manifest_mod, "MANIFEST_DIR", state / "manifests")
    monkeypatch.setattr(manifest_mod, "BACKUP_DIR", state / "backups")
    game, source, workspace = _game(tmp_path, ".rxdata")
    engine = RPGMakerEngine()
    items = engine.unpack(game, workspace)
    originals = {path: path.read_bytes() for path in (game / "Data").iterdir()}
    for item in items:
        item.translated = TARGET
    messages = []
    pipe = Pipeline(meta_callback=lambda key, value: messages.append((key, value)))
    pipe.workspace = SimpleNamespace(root=workspace)
    pipe.manifest = manifest_mod.GameManifest.for_game(game)
    engine._manifest = pipe.manifest
    replace = os.replace

    def fail_second_file(old, new):
        if Path(new).name == "Map001.rxdata":
            raise PermissionError("game map is locked")
        return replace(old, new)

    monkeypatch.setattr("engines.rpgmaker_rgss.os.replace", fail_second_file)

    assert pipe._run_repack_stage(game, engine, items) is False
    assert all(path.read_bytes() == original for path, original in originals.items())
    failure = next(value for key, value in messages if key == "stage_failure")
    assert failure["code"] == "repack_failed"
    assert "game map is locked" in failure["technical_detail"]
    assert not list(game.rglob(".engaixt-rgss-*"))
