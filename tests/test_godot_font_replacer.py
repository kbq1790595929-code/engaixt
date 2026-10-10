from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.font_replacer import build_font_replacement_map_from_pck, get_bundled_fontdata


def _write_minimal_standard_pck(path: Path, resource_path: str, payload: bytes) -> None:
    name = resource_path.encode("utf-8")
    padded_name_len = len(name) + (4 - len(name) % 4) % 4
    entry = bytearray(8 + padded_name_len + 32)
    struct.pack_into("<I", entry, 0, 0x416)
    struct.pack_into("<I", entry, 4, len(name))
    entry[8:8 + len(name)] = name
    struct.pack_into("<Q", entry, 8 + padded_name_len, 0)
    struct.pack_into("<Q", entry, 8 + padded_name_len + 8, len(payload))
    entry[8 + padded_name_len + 16:8 + padded_name_len + 32] = hashlib.md5(payload).digest()

    header = bytearray(48)
    header[:4] = b"GDPC"
    struct.pack_into("<I", header, 4, 2)
    struct.pack_into("<I", header, 8, 4)
    file_base = 48 + 48 + len(entry)
    struct.pack_into("<Q", header, 24, file_base)
    path.write_bytes(bytes(header) + b"\0" * 48 + bytes(entry) + payload)


def test_godot_font_replacer_reads_padded_standard_pck(tmp_path: Path):
    pck = tmp_path / "game.pck"
    font_path = "res://.godot/imported/Latin.ttf-abcdef.fontdata"
    _write_minimal_standard_pck(pck, font_path, b"RSCC" + b"latin-only-font")

    patches = build_font_replacement_map_from_pck(pck)

    assert patches[font_path] == get_bundled_fontdata()
