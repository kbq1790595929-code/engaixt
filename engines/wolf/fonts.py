from __future__ import annotations

import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

from core.open_source_fonts import (
    SOURCE_HAN_SANS_LICENSE,
    find_source_han_sans,
)
from core.resources import resource_path


_FONT_SUFFIXES = {".ttf", ".otf"}
_ALIAS_NAME_IDS = {1, 2, 3, 4, 6, 16, 17}


@dataclass(frozen=True)
class _Table:
    tag: bytes
    data: bytes


@dataclass(frozen=True)
class _NameRecord:
    platform: int
    encoding: int
    language: int
    name_id: int
    text: str


def find_wolf_font_targets(game_dir: Path) -> list[Path]:
    """Find local WOLF text fonts without scanning unrelated asset trees."""
    targets: list[Path] = []
    for root in (game_dir, game_dir / "Data"):
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if (
                path.is_file()
                and path.suffix.casefold() in _FONT_SUFFIXES
                and path.stat().st_size >= 500_000
            ):
                targets.append(path)
    return sorted(set(targets), key=lambda path: str(path).casefold())


def deploy_wolf_cjk_fonts(game_dir: Path, workspace: Path) -> dict[str, object]:
    source = find_source_han_sans()
    targets = find_wolf_font_targets(game_dir)
    result: dict[str, object] = {
        "source": str(source or ""),
        "targets": [],
        "deployed": 0,
        "errors": [],
    }
    if source is None or not targets:
        return result

    source_data = source.read_bytes()
    stage_root = workspace / "wolf" / "font_aliases"
    stage_root.mkdir(parents=True, exist_ok=True)
    for index, target in enumerate(targets):
        try:
            alias_data = build_sfnt_name_alias(source_data, target.read_bytes())
            staged = stage_root / f"{index:02d}_{target.name}"
            staged.write_bytes(alias_data)
            shutil.copy2(staged, target)
            result["targets"].append(str(target))
            result["deployed"] = int(result["deployed"]) + 1
        except Exception as exc:
            result["errors"].append({"path": str(target), "error": str(exc)})

    if result["deployed"]:
        license_source = resource_path("assets", SOURCE_HAN_SANS_LICENSE)
        if license_source.is_file():
            meta = game_dir / "_translation_meta"
            meta.mkdir(parents=True, exist_ok=True)
            shutil.copy2(license_source, meta / SOURCE_HAN_SANS_LICENSE)
    return result


def build_sfnt_name_alias(source_font: bytes, alias_font: bytes) -> bytes:
    """Rename an OFL font to the family names expected by a WOLF game.

    WOLF games commonly bundle Japanese fonts whose cmap contains empty glyphs
    for simplified Chinese. Keeping the original family names lets the engine
    select the same font while Source Han Sans supplies complete outlines.
    """
    version, source_tables = _parse_sfnt(source_font)
    _alias_version, alias_tables = _parse_sfnt(alias_font)
    source_name = _table_data(source_tables, b"name")
    alias_name = _table_data(alias_tables, b"name")
    source_records = _parse_name_table(source_name)
    alias_records = _parse_name_table(alias_name)
    if not source_records or not alias_records:
        raise ValueError("font name table is missing or unsupported")

    renamed = _rename_records(source_records, alias_records)
    name_data = _build_name_table(renamed)
    tables = [
        _Table(table.tag, name_data if table.tag == b"name" else table.data)
        for table in source_tables
    ]
    return _build_sfnt(version, tables)


def _parse_sfnt(data: bytes) -> tuple[bytes, list[_Table]]:
    if len(data) < 12:
        raise ValueError("font header is truncated")
    version = data[:4]
    if version == b"ttcf":
        raise ValueError("font collections are not supported")
    num_tables = struct.unpack_from(">H", data, 4)[0]
    directory_end = 12 + num_tables * 16
    if directory_end > len(data):
        raise ValueError("font table directory is truncated")

    tables: list[_Table] = []
    for index in range(num_tables):
        position = 12 + index * 16
        tag, _checksum, offset, length = struct.unpack_from(">4sIII", data, position)
        if offset + length > len(data):
            raise ValueError(f"font table {tag!r} is truncated")
        tables.append(_Table(tag, data[offset:offset + length]))
    if not any(table.tag == b"head" for table in tables):
        raise ValueError("font head table is missing")
    return version, tables


def _table_data(tables: list[_Table], tag: bytes) -> bytes:
    for table in tables:
        if table.tag == tag:
            return table.data
    raise ValueError(f"font table {tag!r} is missing")


def _parse_name_table(data: bytes) -> list[_NameRecord]:
    if len(data) < 6:
        return []
    _format, count, string_offset = struct.unpack_from(">HHH", data, 0)
    if 6 + count * 12 > len(data) or string_offset > len(data):
        return []
    records: list[_NameRecord] = []
    for index in range(count):
        position = 6 + index * 12
        platform, encoding, language, name_id, length, offset = struct.unpack_from(
            ">HHHHHH", data, position
        )
        start = string_offset + offset
        end = start + length
        if end > len(data):
            continue
        text = _decode_name(data[start:end], platform, encoding)
        if text:
            records.append(_NameRecord(platform, encoding, language, name_id, text))
    return records


def _decode_name(data: bytes, platform: int, encoding: int) -> str:
    try:
        if platform in {0, 3}:
            return data.decode("utf-16-be").rstrip("\x00")
        if platform == 1 and encoding == 0:
            return data.decode("mac_roman").rstrip("\x00")
    except (UnicodeDecodeError, LookupError):
        return ""
    return ""


def _encode_name(text: str, platform: int, encoding: int) -> bytes | None:
    try:
        if platform in {0, 3}:
            return text.encode("utf-16-be")
        if platform == 1 and encoding == 0:
            return text.encode("mac_roman")
    except (UnicodeEncodeError, LookupError):
        return None
    return None


def _rename_records(
    source: list[_NameRecord], alias: list[_NameRecord],
) -> list[_NameRecord]:
    alias_by_key = {
        (record.platform, record.encoding, record.language, record.name_id): record.text
        for record in alias
        if record.name_id in _ALIAS_NAME_IDS
    }
    alias_candidates: dict[tuple[int, int], list[_NameRecord]] = {}
    for record in alias:
        if record.name_id in _ALIAS_NAME_IDS and record.platform in {0, 3}:
            alias_candidates.setdefault((record.platform, record.name_id), []).append(record)

    output: dict[tuple[int, int, int, int], _NameRecord] = {}
    for record in source:
        key = (record.platform, record.encoding, record.language, record.name_id)
        text = record.text
        if record.name_id in _ALIAS_NAME_IDS:
            text = alias_by_key.get(key) or _fallback_alias_text(
                alias_candidates, record.platform, record.name_id, record.language
            )
        output[key] = _NameRecord(*key, text)

    # Add the original font's localized family names. Games can request either
    # the English or Japanese family string through WOLF database settings.
    for record in alias:
        if record.platform not in {0, 3} or record.name_id not in _ALIAS_NAME_IDS:
            continue
        key = (record.platform, record.encoding, record.language, record.name_id)
        output[key] = record
    return sorted(
        output.values(),
        key=lambda record: (record.platform, record.encoding, record.language, record.name_id),
    )


def _fallback_alias_text(
    candidates: dict[tuple[int, int], list[_NameRecord]],
    platform: int,
    name_id: int,
    language: int,
) -> str:
    rows = candidates.get((platform, name_id), [])
    if not rows:
        rows = [
            record
            for (candidate_platform, candidate_name_id), values in candidates.items()
            if candidate_name_id == name_id
            for record in values
        ]
    if not rows:
        return "EngAixt CJK"
    for preferred in (language, 0x0409, 0x0411):
        for record in rows:
            if record.language == preferred:
                return record.text
    return rows[0].text


def _build_name_table(records: list[_NameRecord]) -> bytes:
    encoded_records: list[tuple[_NameRecord, bytes]] = []
    for record in records:
        encoded = _encode_name(record.text, record.platform, record.encoding)
        if encoded is not None:
            encoded_records.append((record, encoded))

    string_offset = 6 + len(encoded_records) * 12
    storage = bytearray()
    offsets: dict[bytes, int] = {}
    directory = bytearray()
    for record, encoded in encoded_records:
        offset = offsets.get(encoded)
        if offset is None:
            offset = len(storage)
            offsets[encoded] = offset
            storage.extend(encoded)
        directory.extend(struct.pack(
            ">HHHHHH",
            record.platform,
            record.encoding,
            record.language,
            record.name_id,
            len(encoded),
            offset,
        ))
    return struct.pack(">HHH", 0, len(encoded_records), string_offset) + bytes(directory) + bytes(storage)


def _build_sfnt(version: bytes, tables: list[_Table]) -> bytes:
    num_tables = len(tables)
    entry_selector = max(0, num_tables.bit_length() - 1)
    search_range = (1 << entry_selector) * 16
    range_shift = num_tables * 16 - search_range
    header = bytearray(version + struct.pack(">HHHH", num_tables, search_range, entry_selector, range_shift))
    directory = bytearray()
    body = bytearray()
    offset = 12 + num_tables * 16
    head_offset = -1

    for table in tables:
        table_data = bytearray(table.data)
        if table.tag == b"head":
            if len(table_data) < 12:
                raise ValueError("font head table is truncated")
            table_data[8:12] = b"\0\0\0\0"
            head_offset = offset
        checksum = _checksum(table_data)
        directory.extend(struct.pack(">4sIII", table.tag, checksum, offset, len(table_data)))
        body.extend(table_data)
        padding = (-len(table_data)) % 4
        if padding:
            body.extend(b"\0" * padding)
        offset += len(table_data) + padding

    if head_offset < 0:
        raise ValueError("font head table is missing")
    output = header + directory + body
    adjustment = (0xB1B0AFBA - _checksum(output)) & 0xFFFFFFFF
    struct.pack_into(">I", output, head_offset + 8, adjustment)
    return bytes(output)


def _checksum(data: bytes | bytearray) -> int:
    padded = bytes(data) + b"\0" * ((-len(data)) % 4)
    total = 0
    for position in range(0, len(padded), 4):
        total = (total + struct.unpack_from(">I", padded, position)[0]) & 0xFFFFFFFF
    return total
