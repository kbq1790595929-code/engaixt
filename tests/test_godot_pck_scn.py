"""Godot binary SCN/RES string extraction regressions."""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from tempfile import TemporaryDirectory

from engines.godot_pck import (
    GodotPckEngine,
    _find_embedded_pck_range,
    _looks_like_godot_steam_pck,
    _parse_steam_entries,
    extract_tscn_strings,
    patch_inline_strings_safe,
    patch_tscn_strings,
    read_pck_payloads,
    rebuild_pck,
    rebuild_pck_steam,
)


def _variant_string(value: str) -> bytes:
    raw = value.encode("utf-8") + b"\x00"
    return struct.pack("<II", 5, len(raw)) + raw


def _steam_entry(name: str, offset: int, data: bytes, flags: int = 0) -> bytes:
    name_bytes = name.encode("utf-8")
    padded = (4 + len(name_bytes) + 3) // 4 * 4
    entry = bytearray(padded + 36)
    struct.pack_into("<I", entry, 0, len(name_bytes))
    entry[4:4 + len(name_bytes)] = name_bytes
    struct.pack_into("<Q", entry, padded, offset)
    struct.pack_into("<Q", entry, padded + 8, len(data))
    entry[padded + 16:padded + 32] = hashlib.md5(data).digest()
    struct.pack_into("<I", entry, padded + 32, flags)
    return bytes(entry)


def _minimal_steam_pck(entries: dict[str, bytes], file_base: int = 64) -> bytes:
    header = bytearray(48)
    header[:4] = b"GDPC"
    struct.pack_into("<I", header, 4, 2)
    struct.pack_into("<Q", header, 24, file_base)
    data = bytearray(header)
    data.extend(b"\x00" * (file_base - len(data)))
    entry_blobs = []
    offset = file_base
    for name, payload in entries.items():
        data.extend(payload)
        entry_blobs.append(_steam_entry(name, offset, payload))
        offset += len(payload)
    for entry in entry_blobs:
        data.extend(entry)
    data_size = len(data)
    data.extend(struct.pack("<Q", data_size))
    data.extend(b"GDPC")
    return bytes(data)


def _standard_entry(name: str, offset: int, data: bytes) -> bytes:
    name_bytes = name.encode("utf-8")
    padded_len = len(name_bytes) + (4 - len(name_bytes) % 4) % 4
    entry = bytearray(8 + padded_len + 32)
    struct.pack_into("<I", entry, 0, 0)
    struct.pack_into("<I", entry, 4, len(name_bytes))
    entry[8:8 + len(name_bytes)] = name_bytes
    meta = 8 + padded_len
    struct.pack_into("<Q", entry, meta, offset)
    struct.pack_into("<Q", entry, meta + 8, len(data))
    entry[meta + 16:meta + 32] = hashlib.md5(data).digest()
    return bytes(entry)


def _minimal_standard_pck(entries: dict[str, bytes]) -> bytes:
    entry_blobs = []
    for name, payload in entries.items():
        entry_blobs.append((name, payload))
    file_base_raw = 48 + sum(len(_standard_entry(name, 0, payload)) for name, payload in entry_blobs)
    file_base = (file_base_raw + 15) & ~15
    header = bytearray(48)
    header[:4] = b"GDPC"
    struct.pack_into("<I", header, 4, 2)
    struct.pack_into("<Q", header, 24, file_base)

    data = bytearray(header)
    offset = 0
    for name, payload in entry_blobs:
        data.extend(_standard_entry(name, offset, payload))
        offset += len(payload)
    data.extend(b"\x00" * (file_base - len(data)))
    for _, payload in entry_blobs:
        data.extend(payload)
    return bytes(data)


def test_godotsteam_pck_detection_and_rebuild_preserve_file_base_padding(tmp_path=None):
    if tmp_path is None:
        with TemporaryDirectory() as tmp:
            return test_godotsteam_pck_detection_and_rebuild_preserve_file_base_padding(Path(tmp))

    source = _minimal_steam_pck({
        "TimeLine/test.dtl": b"old",
        "Other/file.txt": b"keep",
    }, file_base=64)
    src = tmp_path / "game.pck"
    out = tmp_path / "patched.pck"
    src.write_bytes(source)

    assert _looks_like_godot_steam_pck(source)

    rebuild_pck_steam(src, {"TimeLine/test.dtl": b"new text"}, out)
    rebuilt = out.read_bytes()
    entries = _parse_steam_entries(rebuilt)

    assert rebuilt[48:64] == b"\x00" * 16
    assert [(name, size) for name, _, size, _, _ in entries] == [
        ("TimeLine/test.dtl", len(b"new text")),
        ("Other/file.txt", len(b"keep")),
    ]
    assert rebuilt[64:64 + len(b"new text")] == b"new text"


def test_standard_pck_rebuild_matches_res_paths_from_extracted_relative_paths(tmp_path=None):
    if tmp_path is None:
        with TemporaryDirectory() as tmp:
            return test_standard_pck_rebuild_matches_res_paths_from_extracted_relative_paths(Path(tmp))

    source = _minimal_standard_pck({
        "res://Timelines/test.dtl": b"old text",
        "res://Other/file.txt": b"keep",
    })
    src = tmp_path / "game.pck"
    out = tmp_path / "patched.pck"
    src.write_bytes(source)

    rebuild_pck(src, {"Timelines/test.dtl": b"new text"}, out)
    rebuilt = out.read_bytes()
    payloads = read_pck_payloads(out, ["Timelines/test.dtl", "Other/file.txt"])

    assert b"new text" in rebuilt
    assert b"old text" not in rebuilt
    assert b"keep" in rebuilt
    assert payloads["Timelines/test.dtl"] == b"new text"
    assert payloads["Other/file.txt"] == b"keep"


def test_godotsteam_rebuild_matches_res_paths_from_extracted_relative_paths(tmp_path=None):
    if tmp_path is None:
        with TemporaryDirectory() as tmp:
            return test_godotsteam_rebuild_matches_res_paths_from_extracted_relative_paths(Path(tmp))

    source = _minimal_steam_pck({
        "res://Timelines/test.dtl": b"old text",
        "res://Other/file.txt": b"keep",
    }, file_base=64)
    src = tmp_path / "game.pck"
    out = tmp_path / "patched.pck"
    src.write_bytes(source)

    rebuild_pck_steam(src, {"Timelines/test.dtl": b"new text"}, out)
    rebuilt = out.read_bytes()
    entries = _parse_steam_entries(rebuilt)
    payloads = read_pck_payloads(out, ["Timelines/test.dtl", "Other/file.txt"])

    assert [(name, size) for name, _, size, _, _ in entries] == [
        ("res://Timelines/test.dtl", len(b"new text")),
        ("res://Other/file.txt", len(b"keep")),
    ]
    assert b"new text" in rebuilt
    assert b"old text" not in rebuilt
    assert payloads["Timelines/test.dtl"] == b"new text"
    assert payloads["Other/file.txt"] == b"keep"


def test_embedded_godotsteam_pck_range_stops_before_exe_overlay():
    prefix = b"MZ steam exe"
    tail = b"SIGNED_TAIL"
    pck = _minimal_steam_pck({"TimeLine/test.dtl": b"old"}, file_base=64)
    exe = prefix + pck + tail

    assert _find_embedded_pck_range(exe) == (len(prefix), len(prefix) + len(pck))


def test_embedded_standard_pck_range_preserves_exe_overlay_tail(tmp_path=None):
    if tmp_path is None:
        with TemporaryDirectory() as tmp:
            return test_embedded_standard_pck_range_preserves_exe_overlay_tail(Path(tmp))

    prefix = b"MZ fake exe prefix"
    tail = b"AUTHENTICODE_OR_LAUNCHER_OVERLAY"
    pck = _minimal_standard_pck({"TimeLine/test.dtl": b"old"})
    exe = prefix + pck + tail

    assert _find_embedded_pck_range(exe) == (len(prefix), len(prefix) + len(pck))

    game_exe = tmp_path / "Game.exe"
    patched_pck = tmp_path / "patched.pck"
    game_exe.write_bytes(exe)
    patched_pck.write_bytes(_minimal_standard_pck({"TimeLine/test.dtl": b"new"}))

    engine = GodotPckEngine()
    engine._embedded_exe = game_exe
    engine._game_dir = tmp_path
    engine._deploy_rebuilt_pck(tmp_path / "embedded.pck", patched_pck, {})

    rebuilt_exe = game_exe.read_bytes()
    assert rebuilt_exe.startswith(prefix)
    assert rebuilt_exe.endswith(tail)
    start, end = _find_embedded_pck_range(rebuilt_exe)
    assert start == len(prefix)
    assert rebuilt_exe[start:end] == patched_pck.read_bytes()


def test_scn_scan_finds_adjacent_variant_strings_after_non_string_bytes():
    data = (
        b"\x02\x00\x00\x00"
        + _variant_string("タイトルに戻る？")
        + b"\x01\x00\x00\x00\x00\x00\x00\x00"
        + _variant_string("セーブ")
    )

    items = GodotPckEngine()._extract_scn_strings(
        data,
        ".godot/exported/123/export-demo-GameEndWindow.scn",
    )

    originals = {item.original for item in items}
    assert "タイトルに戻る？" in originals
    assert "セーブ" in originals


def test_scn_scan_allows_multiline_text_labels():
    text = (
        "元気いっぱいの兎っぽいもんむす。\n"
        "えっちな事は好きだが性の知識は皆無のようだ。"
    )
    data = _variant_string(text)

    items = GodotPckEngine()._extract_scn_strings(
        data,
        ".godot/exported/123/export-demo-EroStatus.scn",
    )

    assert [item.original for item in items] == [text]


def test_scn_scan_skips_godot_runtime_identifiers():
    data = (
        _variant_string("01_Prologue_A")
        + _variant_string("Morning_01")
        + _variant_string("導入シーンをスキップしますか？")
    )

    items = GodotPckEngine()._extract_scn_strings(
        data,
        ".godot/exported/123/export-demo-01_Prologue_A.res",
    )

    originals = {item.original for item in items}
    assert "01_Prologue_A" not in originals
    assert "Morning_01" not in originals
    assert "導入シーンをスキップしますか？" in originals


def test_scn_patch_does_not_replace_godot_runtime_identifiers_from_checkpoint():
    data = _variant_string("01_Prologue_A")

    patched, shorter, longer, equal = patch_inline_strings_safe(data, {
        "01_Prologue_A": "错误译文",
    })

    assert patched == data
    assert (shorter, longer, equal) == (0, 0, 0)


def test_scn_scan_skips_godot_builtin_type_names():
    # "Script"/"Resource" etc. appear as bare VARIANT_STRING values when they
    # are the ext_resource/sub_resource "type" tag inside binary SCN/RES data.
    # They must never be extracted as translatable dialogue text, otherwise
    # patching them corrupts the resource loader's type tag (regression:
    # ResourceLoader "No loader found ... expected type: 脚本" -> gray screen).
    data = (
        _variant_string("Script")
        + _variant_string("Resource")
        + _variant_string("導入シーンをスキップしますか？")
    )

    items = GodotPckEngine()._extract_scn_strings(
        data,
        ".godot/exported/456/export-demo-vn_choice_layer.scn",
    )

    originals = {item.original for item in items}
    assert "Script" not in originals
    assert "Resource" not in originals
    assert "導入シーンをスキップしますか？" in originals


def test_scn_patch_does_not_replace_godot_builtin_type_names_from_checkpoint():
    data = _variant_string("Script")

    patched, shorter, longer, equal = patch_inline_strings_safe(data, {
        "Script": "脚本",
    })

    assert patched == data
    assert (shorter, longer, equal) == (0, 0, 0)


def test_tscn_extract_and_patch_skip_runtime_identifiers():
    content = (
        'label = "Talk_A_1"\n'
        'name = "01_Prologue_A"\n'
        'timeline = "Morning_01"\n'
        'text = "導入シーンをスキップしますか？"\n'
    )

    items = extract_tscn_strings(content, "Resource/DialogueEvent/01_Main/01_Prologue_A.tres")
    assert [item.original for item in items] == ["導入シーンをスキップしますか？"]

    patched, count = patch_tscn_strings(content, {
        "Talk_A_1": "坏译文",
        "01_Prologue_A": "坏译文",
        "Morning_01": "坏译文",
        "導入シーンをスキップしますか？": "要跳过导入场景吗？",
    })

    assert count == 1
    assert 'label = "Talk_A_1"' in patched
    assert 'name = "01_Prologue_A"' in patched
    assert 'timeline = "Morning_01"' in patched
    assert 'text = "要跳过导入场景吗？"' in patched
