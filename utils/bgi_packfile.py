"""BGI/Ethornell ``PackFile`` archive helpers.

This format is used by several Ethornell/August titles for scenario, image,
and audio archives. Each directory entry is 32 bytes: a 16-byte CP932 name,
relative payload offset, payload size, and 8 reserved bytes.
"""
from __future__ import annotations

import struct


PACKFILE_MAGIC = b"PackFile    "


def parse_packfile(data: bytes) -> list[tuple[str, int, int, bytes]]:
    """Parse a BGI ``PackFile`` archive.

    Returns ``(name, offset, size, entry_data)`` tuples. ``offset`` is relative
    to the payload section immediately following the directory table.
    """
    if len(data) < 16 or data[:12] != PACKFILE_MAGIC:
        return []

    file_count = struct.unpack_from("<I", data, 0x0C)[0]
    if file_count <= 0 or file_count > 100000:
        return []

    data_offset = 16 + file_count * 32
    if data_offset > len(data):
        return []

    entries: list[tuple[str, int, int, bytes]] = []
    for index in range(file_count):
        pos = 16 + index * 32
        raw_name = data[pos:pos + 16].split(b"\x00")[0]
        try:
            name = raw_name.decode("cp932", errors="replace")
        except Exception:
            name = raw_name.decode("ascii", errors="replace")

        offset = struct.unpack_from("<I", data, pos + 0x10)[0]
        size = struct.unpack_from("<I", data, pos + 0x14)[0]
        if size <= 0:
            continue
        start = data_offset + offset
        if start + size > len(data):
            return []
        entries.append((name, offset, size, data[start:start + size]))
    return entries


def build_packfile(entries: list[tuple[str, bytes]]) -> bytes:
    """Build a BGI ``PackFile`` archive from ``(name, data)`` entries."""
    header = bytearray(PACKFILE_MAGIC)
    header += struct.pack("<I", len(entries))

    offset = 0
    payload = bytearray()
    for name, data in entries:
        raw_name = name.encode("cp932", errors="replace")[:16]
        header += raw_name + b"\x00" * (16 - len(raw_name))
        header += struct.pack("<II", offset, len(data))
        header += b"\x00" * 8
        payload += data
        offset += len(data)

    return bytes(header + payload)
