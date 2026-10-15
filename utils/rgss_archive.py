"""Standard RGSSAD v1 (XP/VX) and v3 (VX Ace) archive read/write.

Format reference: RPG-Maker-Translation-Tools/rpgm-archive-decrypter-lib.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from utils.rgss3a import _xor_data


@dataclass(frozen=True)
class RGSSEntry:
    name: str
    raw_name: bytes
    data: bytes
    key: int = 0


@dataclass(frozen=True)
class RGSSArchive:
    version: int
    entries: tuple[RGSSEntry, ...]
    base_key: int = 0


def safe_relative_name(name: str) -> str:
    win_path = PureWindowsPath(name)
    normalized = name.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not name or win_path.drive or win_path.root or win_path.is_reserved()
        or any(part in ("", ".", "..") or part != part.rstrip(". ") for part in parts)
        or any(char in name for char in ('\0', ':', '<', '>', '"', '|', '?', '*'))
    ):
        raise ValueError(f"Unsafe RGSS path: {name!r}")
    return "/".join(parts)


def _decode_name(raw: bytes) -> str:
    for encoding in ("utf-8", "cp932"):
        try:
            return safe_relative_name(raw.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ValueError("RGSS filename has an unsupported encoding")


def _step(key: int) -> int:
    return (key * 7 + 3) & 0xFFFFFFFF


def read_rgss_archive(data: bytes) -> RGSSArchive:
    if len(data) < 8 or data[:7] != b"RGSSAD\0":
        raise ValueError("Invalid or truncated RGSSAD header")
    version = data[7]
    if version not in (1, 3):
        raise ValueError(f"Unsupported RGSSAD version: {version}")
    entries: list[RGSSEntry] = []
    pos = 8

    def uint32() -> int:
        nonlocal pos
        if pos + 4 > len(data):
            raise ValueError("Truncated RGSS archive entry")
        value = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        return value

    def take(size: int) -> bytes:
        nonlocal pos
        if size > len(data) - pos:
            raise ValueError("Truncated RGSS archive payload")
        value = data[pos:pos + size]
        pos += size
        return value

    if version == 1:
        key = 0xDEADCAFE
        while pos < len(data):
            name_size = uint32() ^ key
            key = _step(key)
            if not 0 < name_size <= 4096:
                raise ValueError("Invalid RGSS archive filename length")
            raw_name = bytearray(take(name_size))
            for index in range(len(raw_name)):
                raw_name[index] ^= key & 0xFF
                key = _step(key)
            size = uint32() ^ key
            key = _step(key)
            payload = bytes(_xor_data(key, take(size)))
            entries.append(RGSSEntry(_decode_name(bytes(raw_name)), bytes(raw_name), payload, key))
        archive = RGSSArchive(version, tuple(entries))
    else:
        base_key = uint32()
        key = (base_key * 9 + 3) & 0xFFFFFFFF
        table: list[tuple[int, int, int, bytes]] = []
        while True:
            offset = uint32() ^ key
            if offset == 0:
                break
            size, file_key, name_size = (uint32() ^ key for _ in range(3))
            if not 0 < name_size <= 4096:
                raise ValueError("Invalid RGSS archive filename length")
            raw = take(name_size)
            key_bytes = struct.pack("<I", key)
            raw_name = bytes(value ^ key_bytes[index % 4] for index, value in enumerate(raw))
            table.append((offset, size, file_key, raw_name))
        minimum = pos
        for offset, size, file_key, raw_name in table:
            if offset < minimum or size > len(data) - offset:
                raise ValueError("Invalid or overlapping RGSS archive offsets")
            payload = bytes(_xor_data(file_key, data[offset:offset + size]))
            entries.append(RGSSEntry(_decode_name(raw_name), raw_name, payload, file_key))
            minimum = offset + size
        archive = RGSSArchive(version, tuple(entries), base_key)

    names = [entry.name.casefold() for entry in entries]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate or case-aliased RGSS archive paths")
    return archive


def rebuild_rgss_archive(archive: RGSSArchive, replacements: dict[str, bytes]) -> bytes:
    unknown = replacements.keys() - {entry.name for entry in archive.entries}
    if unknown:
        raise ValueError(f"RGSS replacements reference missing entries: {sorted(unknown)!r}")
    output = bytearray(b"RGSSAD\0" + bytes([archive.version]))
    if archive.version == 1:
        key = 0xDEADCAFE
        for entry in archive.entries:
            payload = replacements.get(entry.name, entry.data)
            output.extend(struct.pack("<I", len(entry.raw_name) ^ key))
            key = _step(key)
            for value in entry.raw_name:
                output.append(value ^ (key & 0xFF))
                key = _step(key)
            output.extend(struct.pack("<I", len(payload) ^ key))
            key = _step(key)
            output.extend(_xor_data(key, payload))
    elif archive.version == 3:
        output.extend(struct.pack("<I", archive.base_key))
        key = (archive.base_key * 9 + 3) & 0xFFFFFFFF
        key_bytes = struct.pack("<I", key)
        offset = 12 + sum(16 + len(entry.raw_name) for entry in archive.entries) + 16
        payloads: list[bytes] = []
        for entry in archive.entries:
            payload = replacements.get(entry.name, entry.data)
            output.extend(struct.pack("<IIII", offset ^ key, len(payload) ^ key,
                                      entry.key ^ key, len(entry.raw_name) ^ key))
            output.extend(value ^ key_bytes[index % 4] for index, value in enumerate(entry.raw_name))
            payloads.append(bytes(_xor_data(entry.key, payload)))
            offset += len(payload)
        output.extend(struct.pack("<IIII", key, key, key, key))
        for payload in payloads:
            output.extend(payload)
    else:
        raise ValueError(f"Unsupported RGSSAD version: {archive.version}")
    return bytes(output)


def extract_rgss_archive(archive: RGSSArchive, destination: Path) -> None:
    root = destination.resolve()
    targets = [(entry, root / safe_relative_name(entry.name)) for entry in archive.entries]
    if any(not target.resolve().is_relative_to(root) for _, target in targets):
        raise ValueError("RGSS output path escapes extraction directory")
    for entry, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(entry.data)
