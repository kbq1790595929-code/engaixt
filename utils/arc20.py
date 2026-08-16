"""BURIKO ARC20 archive helpers.

ARC20 entry offsets are relative to the data section, not to the beginning of
the archive. Some older notes describe them as absolute offsets; keep a
fallback for such variants, but prefer the relative interpretation.
"""
from __future__ import annotations

import struct
from pathlib import Path


def parse_arc20(data: bytes) -> list[tuple[str, int, int, bytes]]:
    """Parse BURIKO ARC20 archive.

    Returns list of (filename, offset, size, entry_data) tuples. ``offset`` is
    the raw entry offset value from the archive index.
    """
    if len(data) < 16 or data[:12] != b"BURIKO ARC20":
        return []

    file_count = struct.unpack_from("<I", data, 0x0C)[0]
    if file_count <= 0 or file_count > 100000:
        return []

    index_size = 16 + file_count * 128
    if index_size > len(data):
        return []

    entries: list[tuple[str, int, int, bytes]] = []
    for i in range(file_count):
        pos = 16 + i * 128

        raw_name = data[pos:pos + 96].split(b"\x00")[0]
        try:
            name = raw_name.decode("cp932", errors="replace")
        except Exception:
            name = raw_name.decode("ascii", errors="replace")

        offset = struct.unpack_from("<I", data, pos + 0x60)[0]
        size = struct.unpack_from("<I", data, pos + 0x64)[0]
        if size <= 0:
            continue

        start = index_size + offset
        if start + size <= len(data):
            entries.append((name, offset, size, data[start:start + size]))
            continue

        # Compatibility fallback for uncommon absolute-offset archives.
        if offset + size <= len(data):
            entries.append((name, offset, size, data[offset:offset + size]))

    return entries


def build_arc20(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a BURIKO ARC20 archive from (filename, data) entries."""
    header = bytearray()
    header += b"BURIKO ARC20"
    header += struct.pack("<I", len(entries))

    offset = 0
    payload = bytearray()
    for name, data in entries:
        raw_name = name.encode("cp932", errors="replace")[:95]
        name_field = raw_name + b"\x00" * (96 - len(raw_name))
        header += name_field
        header += struct.pack("<II", offset, len(data))
        header += b"\x00" * 24
        payload += data
        offset += len(data)

    return bytes(header + payload)


def extract_arc20(data: bytes, output_dir: Path) -> int:
    """Extract all files from ARC20 data. Returns file count."""
    entries = parse_arc20(data)
    output_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, _, _, entry_data in entries:
        safe_name = name.replace("\\", "_").replace("/", "_")
        (output_dir / safe_name).write_bytes(entry_data)
        count += 1
    return count


def extract_all_arc20(game_dir: Path, output_dir: Path) -> int:
    """Find and extract all ARC20 archives in game_dir. Returns total file count."""
    total = 0
    for arc_file in sorted(game_dir.rglob("*.arc")):
        try:
            data = arc_file.read_bytes()
            if data[:12] == b"BURIKO ARC20":
                arc_out = output_dir / arc_file.stem
                total += extract_arc20(data, arc_out)
        except Exception:
            pass
    return total


def is_encrypted_script(data: bytes) -> bool:
    """Heuristic for BGI script-like payloads."""
    if data.startswith(b"DSC FORMAT 1.00\x00"):
        return False
    if data[:0x1C] == b"BurikoCompiledScriptVer1.00\x00":
        return False
    if len(data) < 4:
        return False
    first_dw = struct.unpack_from("<I", data, 0)[0]
    return not (0 < first_dw < 0x600)
