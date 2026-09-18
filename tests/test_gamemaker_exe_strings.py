"""GameMaker executable string extraction regression tests."""

from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.gamemaker import (
    GameMakerEngine,
    _fit_executable_translation,
    _looks_like_executable_game_text,
    _patch_executable_strings,
    _read_pe_data_strings,
)
from engines.base import TextItem


def _build_minimal_pe(path: Path):
    data = bytearray(0x800)
    data[:2] = b"MZ"
    pe_offset = 0x80
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset:pe_offset + 4] = b"PE\0\0"

    section_count = 2
    opt_header_size = 0xE0
    struct.pack_into(
        "<HHIIIHH",
        data,
        pe_offset + 4,
        0x14C,
        section_count,
        0,
        0,
        0,
        opt_header_size,
        0x010F,
    )

    section_table = pe_offset + 24 + opt_header_size
    _write_section(data, section_table, b".text", 0x200, 0x200, 0x60000020)
    _write_section(data, section_table + 40, b".data", 0x400, 0x200, 0xC0000040)

    data[0x200:0x220] = b"Should Not Scan Code\0"
    data[0x400:0x460] = b"Previous Weapon\0Next Weapon\0obj_player\0C:\\Windows\\bad\0"
    path.write_bytes(data)


def _build_minimal_gamemaker_dir(root: Path):
    _build_minimal_pe(root / "Demo.exe")
    data = bytearray((root / "Demo.exe").read_bytes())
    data[0x470:0x47f] = b"PLAY\0PLAY\0QUIT\0"
    (root / "Demo.exe").write_bytes(data)
    gm = bytearray()
    gm.extend(b"FORM")
    gm.extend((12).to_bytes(4, "big"))
    gm.extend(b"GEN8")
    (root / "data.win").write_bytes(gm)


def _write_section(data: bytearray, offset: int, name: bytes, raw_ptr: int,
                   raw_size: int, characteristics: int):
    data[offset:offset + 8] = name.ljust(8, b"\0")
    struct.pack_into(
        "<IIIIIIHHI",
        data,
        offset + 8,
        raw_size,
        raw_ptr,
        raw_size,
        raw_ptr,
        0,
        0,
        0,
        0,
        characteristics,
    )


def test_reads_only_writable_non_code_pe_sections():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "Demo.exe"
        _build_minimal_pe(exe)
        values = [(section, text) for section, _offset, text in _read_pe_data_strings(exe)]

    assert (".data", "Previous Weapon") in values
    assert (".data", "Next Weapon") in values
    assert (".text", "Should Not Scan Code") not in values


def test_executable_game_text_filter_keeps_ui_and_rejects_technical_ids():
    assert _looks_like_executable_game_text("Previous Weapon")
    assert _looks_like_executable_game_text("Chat Status")
    assert _looks_like_executable_game_text("PLAY")
    assert _looks_like_executable_game_text("SETTINGS")
    assert _looks_like_executable_game_text("PATCH NOTES")
    assert _looks_like_executable_game_text("CREDITS")
    assert _looks_like_executable_game_text("QUIT")
    assert not _looks_like_executable_game_text("obj_player")
    assert not _looks_like_executable_game_text("PLAYER_CLOUD")
    assert not _looks_like_executable_game_text(r"C:\Windows\bad")


def test_executable_extract_keeps_duplicate_known_ui_slots():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_minimal_gamemaker_dir(root)
        items = GameMakerEngine()._extract_executable_strings(root, set())
        play_items = [item for item in items if item.original == "PLAY"]

    assert len(play_items) == 2
    assert {item.meta["offset"] for item in play_items} == {0x470, 0x475}


def test_manual_exe_is_used_for_executable_string_extraction():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_minimal_gamemaker_dir(root)
        _build_minimal_pe(root / "Manual.exe")
        data = bytearray((root / "Manual.exe").read_bytes())
        data[0x470:0x482] = b"PATCH NOTES\0QUIT\0"
        (root / "Manual.exe").write_bytes(data)

        items = GameMakerEngine().unpack(root / "Manual.exe", root / "workspace")
        originals = {item.original for item in items}

    assert "PATCH NOTES" in originals
    assert "PLAY" not in originals


def test_patches_shorter_utf8_translation_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "Demo.exe"
        _build_minimal_pe(exe)
        items = [
            TextItem(
                file="Demo.exe",
                key="PE[.data]@0x400",
                original="Previous Weapon",
                translated="上一武器",
                meta={
                    "source_kind": "executable_string",
                    "source": str(exe),
                    "offset": 0x400,
                    "byte_len": len("Previous Weapon".encode("utf-8")),
                },
            )
        ]

        stats = _patch_executable_strings(items)
        data = exe.read_bytes()

    assert stats["patched"] == 1
    assert "上一武器".encode("utf-8") in data
    assert b"Next Weapon\0" in data


def test_skips_too_long_utf8_translation():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "Demo.exe"
        _build_minimal_pe(exe)
        items = [
            TextItem(
                file="Demo.exe",
                key="PE[.data]@0x40F",
                original="Next Weapon",
                translated="这是一个很长很长的翻译",
                meta={
                    "source_kind": "executable_string",
                    "source": str(exe),
                    "offset": 0x400 + len(b"Previous Weapon\0"),
                    "byte_len": len("Next Weapon".encode("utf-8")),
                },
            )
        ]

        stats = _patch_executable_strings(items)
        data = exe.read_bytes()

    assert stats["patched"] == 0
    assert stats["skipped_too_long"] == 1
    assert b"Next Weapon\0" in data


def test_shortens_common_ui_translation_to_fit_fixed_slot():
    assert _fit_executable_translation("Use", "使用", 3) == "用"
    assert _fit_executable_translation("Map", "地图", 3) == "图"
    assert _fit_executable_translation("Aim X", "瞄准 X", 5) == "瞄X"
    assert _fit_executable_translation("PLAY", "游玩", 4) == "玩"
    assert _fit_executable_translation("PATCH NOTES", "补丁说明", 11) == "补丁"
    assert _fit_executable_translation("CREDITS", "制作人员", 7) == "制作"


def test_patches_shortened_ui_translation_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "Demo.exe"
        _build_minimal_pe(exe)
        data = bytearray(exe.read_bytes())
        offset = 0x470
        data[offset:offset + 4] = b"Use\0"
        exe.write_bytes(data)
        items = [
            TextItem(
                file="Demo.exe",
                key="PE[.data]@0x470",
                original="Use",
                translated="使用",
                meta={
                    "source_kind": "executable_string",
                    "source": str(exe),
                    "offset": offset,
                    "byte_len": 3,
                },
            )
        ]

        stats = _patch_executable_strings(items)
        data = exe.read_bytes()

    assert stats["patched"] == 1
    assert "用".encode("utf-8") in data
    assert "使用".encode("utf-8") not in data
    assert items[0].translated == "用"
    assert items[0].meta["repack_note"] == "translation_shortened_to_fit_fixed_slot"


if __name__ == "__main__":
    test_reads_only_writable_non_code_pe_sections()
    test_executable_game_text_filter_keeps_ui_and_rejects_technical_ids()
    test_executable_extract_keeps_duplicate_known_ui_slots()
    test_manual_exe_is_used_for_executable_string_extraction()
    test_patches_shorter_utf8_translation_in_place()
    test_skips_too_long_utf8_translation()
    test_shortens_common_ui_translation_to_fit_fixed_slot()
    test_patches_shortened_ui_translation_in_place()
    print("GameMaker exe string tests passed")
