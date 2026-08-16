"""XP3 容器：索引读取、补丁包写入、原槽回填与脚本包选择。"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from engines.base import TextItem
from utils.kirikiri_psb import is_kirikiri_scn

from engines.kirikiri.codec import (
    _apply_koihazi_xp3dec_filter,
    _decode_script_bytes_for_quality,
    _is_probably_binary_script,
    _is_probably_protected_text_script,
    _norm_rel,
    _try_decode_xp3_single_byte_xor_filter,
    _xor_bytes,
)


XP3_SIGNATURE = b"XP3\x0d\x0a\x20\x0a\x1a\x8b\x67\x01"


@dataclass(frozen=True)
class _Xp3Segment:
    flags: int
    offset: int
    original_size: int
    stored_size: int


@dataclass(frozen=True)
class _Xp3Entry:
    name: str
    original_size: int
    stored_size: int
    segments: tuple[_Xp3Segment, ...]
    adler: int | None = None


@dataclass(frozen=True)
class _Xp3Index:
    entries: tuple[_Xp3Entry, ...]
    unknown_chunks: tuple[str, ...]
    chained: bool


def _collect_xp3_patch_filters(
    changed: dict[str, list[TextItem]],
    *,
    neutralize_koihazi: bool = False,
) -> dict[str, dict[str, object]]:
    filters: dict[str, dict[str, object]] = {}
    for rel, file_items in changed.items():
        rel = _norm_rel(rel)
        for item in file_items:
            xp3_filter = (item.meta or {}).get("xp3_filter")
            if isinstance(xp3_filter, dict):
                if neutralize_koihazi and str(xp3_filter.get("kind") or "") == "koihazi_xp3dec":
                    filters[rel] = {
                        "kind": "koihazi_xp3dec",
                        "file_hash": 0,
                        "neutralized": True,
                    }
                    break
                filters[rel] = dict(xp3_filter)
                break
    return filters


def _apply_xp3_patch_filter(
    data: bytes,
    xp3_filter: dict[str, object] | None,
    *,
    file_hash_override: int | None = None,
) -> bytes:
    if not xp3_filter:
        return data
    kind = str(xp3_filter.get("kind") or "")
    if kind == "koihazi_xp3dec":
        if bool(xp3_filter.get("neutralized")):
            return data
        try:
            file_hash = int(file_hash_override if file_hash_override is not None else xp3_filter.get("file_hash"))
            return _apply_koihazi_xp3dec_filter(data, file_hash)
        except (TypeError, ValueError):
            return data
    if kind != "single_byte_xor":
        return data
    try:
        key = int(xp3_filter.get("key"))
    except (TypeError, ValueError):
        return data
    return _xor_bytes(data, key)


def _xp3_filter_adler_override(data: bytes, xp3_filter: dict[str, object] | None) -> int | None:
    if not xp3_filter:
        return None
    if str(xp3_filter.get("kind") or "") == "koihazi_xp3dec":
        if bool(xp3_filter.get("neutralized")):
            return 0
        return zlib.adler32(data) & 0xFFFFFFFF
    return None


def _write_xp3_patch(
    out_path: Path,
    root: Path,
    files: list[Path],
    *,
    filters_by_rel: dict[str, dict[str, object]] | None = None,
    include_basename_aliases: bool = False,
) -> None:
    entries: list[tuple[str, bytes, int | None]] = []
    seen: set[str] = set()
    filters_by_rel = filters_by_rel or {}
    basename_counts: dict[str, int] = {}
    normalized_files: list[tuple[Path, str]] = []
    for file_path in sorted(files, key=lambda p: _norm_rel(p.relative_to(root)).lower()):
        rel = _norm_rel(file_path.relative_to(root))
        if file_path.name.lower() == "patch.xp3":
            continue
        normalized_files.append((file_path, rel))
        basename_counts[Path(rel).name.lower()] = basename_counts.get(Path(rel).name.lower(), 0) + 1

    for file_path, rel in normalized_files:
        if rel in seen:
            continue
        seen.add(rel)
        data = file_path.read_bytes()
        xp3_filter = filters_by_rel.get(rel)
        adler_override = _xp3_filter_adler_override(data, xp3_filter)
        patched = _apply_xp3_patch_filter(data, xp3_filter, file_hash_override=adler_override)
        entries.append((rel, patched, adler_override))
        basename = Path(rel).name
        basename_key = basename.lower()
        if include_basename_aliases and "/" in rel and basename_counts.get(basename_key) == 1 and basename_key not in seen:
            seen.add(basename_key)
            entries.append((basename, patched, adler_override))
    if not entries:
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    file_entries: list[bytes] = []
    with out_path.open("wb") as out:
        out.write(XP3_SIGNATURE)
        out.write(struct.pack("<Q", 0))
        for rel, data, adler_override in entries:
            offset = out.tell()
            stored, compressed = _maybe_compress(data)
            out.write(stored)
            file_entries.append(_xp3_file_entry(rel, data, offset, len(stored), compressed, adler_override=adler_override))

        index = b"".join(file_entries)
        compressed_index = zlib.compress(index, level=9)
        index_offset = out.tell()
        if len(compressed_index) + 17 < len(index) + 9:
            out.write(struct.pack("<BQQ", 1, len(compressed_index), len(index)))
            out.write(compressed_index)
        else:
            out.write(struct.pack("<BQ", 0, len(index)))
            out.write(index)

        out.seek(len(XP3_SIGNATURE))
        out.write(struct.pack("<Q", index_offset))


def _rewrite_xp3_with_replacements(source_path: Path, out_path: Path, replacements: dict[str, bytes]) -> int:
    index = _read_xp3_index(source_path)
    replacement_map = {_norm_rel(name): data for name, data in replacements.items()}
    replaced = 0
    file_entries: list[bytes] = []
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as out:
        out.write(XP3_SIGNATURE)
        out.write(struct.pack("<Q", 0))
        for entry in index.entries:
            rel = _norm_rel(entry.name)
            if rel in replacement_map:
                data = replacement_map[rel]
                replaced += 1
            else:
                data = _read_xp3_entry_bytes(source_path, entry)
            offset = out.tell()
            stored, compressed = _maybe_compress(data)
            out.write(stored)
            file_entries.append(_xp3_file_entry(entry.name, data, offset, len(stored), compressed))

        raw_index = b"".join(file_entries)
        compressed_index = zlib.compress(raw_index, level=9)
        index_offset = out.tell()
        if len(compressed_index) + 17 < len(raw_index) + 9:
            out.write(struct.pack("<BQQ", 1, len(compressed_index), len(raw_index)))
            out.write(compressed_index)
        else:
            out.write(struct.pack("<BQ", 0, len(raw_index)))
            out.write(raw_index)

        out.seek(len(XP3_SIGNATURE))
        out.write(struct.pack("<Q", index_offset))
    return replaced


def _append_xp3_replacement_index(path: Path, replacements: dict[str, bytes]) -> int:
    index = _read_xp3_index(path)
    raw_chunks = _read_xp3_index_chunks(path)
    chain_next_pointer = _xp3_first_index_next_pointer_offset(path) if index.chained else None
    replacement_map = {_norm_rel(name): data for name, data in replacements.items()}
    replaced = 0
    index_chunks: list[bytes] = []

    with path.open("r+b") as f:
        f.seek(0, 2)
        append_offset = f.tell()
        for tag, body in raw_chunks:
            if tag == b"File":
                entry = _parse_xp3_file_entry(body)
                rel = _norm_rel(entry.name) if entry is not None else ""
            else:
                entry = None
                rel = ""

            if entry is not None and rel in replacement_map:
                data = replacement_map[rel]
                stored, compressed = _maybe_compress(data)
                offset = f.tell()
                f.write(stored)
                replaced += 1
                index_chunks.append(_xp3_file_entry(entry.name, data, offset, len(stored), compressed))
            else:
                index_chunks.append(tag + struct.pack("<Q", len(body)) + body)

        if replaced <= 0:
            f.truncate(append_offset)
            return 0

        raw_index = b"".join(index_chunks)
        compressed_index = zlib.compress(raw_index, level=9)
        index_offset = f.tell()
        if len(compressed_index) + 17 < len(raw_index) + 9:
            f.write(struct.pack("<BQQ", 1, len(compressed_index), len(raw_index)))
            f.write(compressed_index)
        else:
            f.write(struct.pack("<BQ", 0, len(raw_index)))
            f.write(raw_index)

        f.seek(chain_next_pointer if chain_next_pointer is not None else len(XP3_SIGNATURE))
        f.write(struct.pack("<Q", index_offset))
    return replaced


def _patch_xp3_replacements_in_original_slots(path: Path, replacements: dict[str, bytes]) -> dict[str, object]:
    """Patch replacement payloads into existing XP3 segments without changing the index.

    Some protected KiriKiri builds are sensitive to appended replacement index
    chunks. For filtered UTF-16 scripts that shrink after ASCII placeholder
    replacement, we can keep the original entry metadata intact by padding the
    script back to its original uncompressed size and overwriting the original
    segment bytes in place.
    """
    index = _read_xp3_index(path)
    entries = {_norm_rel(entry.name): entry for entry in index.entries}
    replacement_map = {_norm_rel(name): data for name, data in replacements.items()}
    prepared: list[tuple[_Xp3Segment, bytes, str]] = []
    skipped: list[dict[str, str]] = []

    for rel, data in replacement_map.items():
        entry = entries.get(rel)
        if entry is None:
            skipped.append({"file": rel, "reason": "entry_missing"})
            continue
        if len(entry.segments) != 1:
            skipped.append({"file": rel, "reason": "multi_segment"})
            continue
        segment = entry.segments[0]
        padded = _pad_kirikiri_filtered_payload_for_slot(data, entry.original_size, entry)
        if padded is None:
            skipped.append({"file": rel, "reason": "cannot_pad_to_original_size"})
            continue
        if segment.flags & 1:
            stored = zlib.compress(padded, level=9)
            if len(stored) > segment.stored_size:
                skipped.append({"file": rel, "reason": "compressed_payload_too_large"})
                continue
            stored = stored + (b"\x00" * (segment.stored_size - len(stored)))
        else:
            if len(padded) != segment.stored_size:
                skipped.append({"file": rel, "reason": "uncompressed_size_mismatch"})
                continue
            stored = padded
        prepared.append((segment, stored, rel))

    if not prepared:
        return {"replaced": 0, "skipped": skipped}

    with path.open("r+b") as f:
        for segment, stored, _rel in prepared:
            f.seek(segment.offset)
            f.write(stored)
    return {
        "replaced": len(prepared),
        "replaced_files": [rel for _segment, _stored, rel in prepared],
        "skipped": skipped,
    }


def _pad_kirikiri_filtered_payload_for_slot(
    data: bytes,
    original_size: int,
    entry: _Xp3Entry | None = None,
) -> bytes | None:
    if len(data) > original_size:
        return None
    if len(data) == original_size:
        return data
    if entry is not None and entry.adler is not None:
        padded = _pad_koihazi_xp3dec_payload_for_slot(data, original_size, entry.adler)
        if padded is not None:
            return padded
    needed = original_size - len(data)
    if needed % 2:
        return None
    decoded = _try_decode_xp3_single_byte_xor_filter(data)
    if decoded is None or decoded.decoded.encoding != "utf-16":
        return None
    plain = _xor_bytes(data, decoded.key)
    plain += (" " * (needed // 2)).encode("utf-16le")
    if len(plain) != original_size:
        return None
    return _xor_bytes(plain, decoded.key)


def _pad_koihazi_xp3dec_payload_for_slot(data: bytes, original_size: int, file_hash: int) -> bytes | None:
    plain = _apply_koihazi_xp3dec_filter(data, file_hash)
    decoded = _decode_script_bytes_for_quality(plain)
    if decoded is None:
        return None
    needed = original_size - len(plain)
    if needed < 0:
        return None
    encoding = decoded.encoding
    if encoding in {"utf-16", "utf-16le", "utf-16be"}:
        if needed % 2:
            return None
        padding = (" " * (needed // 2)).encode("utf-16le")
    else:
        padding = b" " * needed
    padded_plain = plain + padding
    if len(padded_plain) != original_size:
        return None
    return _apply_koihazi_xp3dec_filter(padded_plain, file_hash)


def _read_xp3_index_chunks(path: Path) -> list[tuple[bytes, bytes]]:
    raw_index_parts: list[bytes] = []
    with path.open("rb") as f:
        if f.read(len(XP3_SIGNATURE)) != XP3_SIGNATURE:
            raise ValueError("not an XP3 archive")
        index_offset = _read_u64(f)
        while index_offset:
            if index_offset < len(XP3_SIGNATURE) or index_offset >= path.stat().st_size:
                raise ValueError(f"invalid XP3 index offset: {index_offset}")
            f.seek(index_offset)
            flag_data = f.read(1)
            if not flag_data:
                raise ValueError("missing XP3 index flag")
            flag = flag_data[0]
            index_kind = flag & 0x7F
            if index_kind == 0:
                size = _read_u64(f)
                raw_index_parts.append(f.read(size))
            elif index_kind == 1:
                compressed_size = _read_u64(f)
                uncompressed_size = _read_u64(f)
                data = zlib.decompress(f.read(compressed_size))
                if len(data) != uncompressed_size:
                    raise ValueError("XP3 index size mismatch")
                raw_index_parts.append(data)
            else:
                raise ValueError(f"unsupported XP3 index flag: {flag}")

            if flag & 0x80:
                index_offset = _read_u64(f)
            else:
                index_offset = 0
    return list(_iter_xp3_chunks(b"".join(raw_index_parts)))


def _xp3_first_index_next_pointer_offset(path: Path) -> int | None:
    with path.open("rb") as f:
        if f.read(len(XP3_SIGNATURE)) != XP3_SIGNATURE:
            return None
        index_offset = _read_u64(f)
        if not index_offset:
            return None
        f.seek(index_offset)
        flag_data = f.read(1)
        if not flag_data or not (flag_data[0] & 0x80):
            return None
        index_kind = flag_data[0] & 0x7F
        if index_kind == 0:
            size = _read_u64(f)
            f.seek(size, 1)
        elif index_kind == 1:
            compressed_size = _read_u64(f)
            _uncompressed_size = _read_u64(f)
            f.seek(compressed_size, 1)
        else:
            return None
        return f.tell()


def _read_xp3_index(path: Path) -> _Xp3Index:
    with path.open("rb") as f:
        if f.read(len(XP3_SIGNATURE)) != XP3_SIGNATURE:
            raise ValueError("not an XP3 archive")
        index_offset = _read_u64(f)
        chunks: list[bytes] = []
        chained = False
        while index_offset:
            if index_offset < len(XP3_SIGNATURE) or index_offset >= path.stat().st_size:
                raise ValueError(f"invalid XP3 index offset: {index_offset}")
            f.seek(index_offset)
            flag_data = f.read(1)
            if not flag_data:
                raise ValueError("missing XP3 index flag")
            flag = flag_data[0]
            index_kind = flag & 0x7F
            if index_kind == 0:
                size = _read_u64(f)
                chunks.append(f.read(size))
            elif index_kind == 1:
                compressed_size = _read_u64(f)
                uncompressed_size = _read_u64(f)
                data = zlib.decompress(f.read(compressed_size))
                if len(data) != uncompressed_size:
                    raise ValueError("XP3 index size mismatch")
                chunks.append(data)
            else:
                raise ValueError(f"unsupported XP3 index flag: {flag}")

            if flag & 0x80:
                chained = True
                index_offset = _read_u64(f)
            else:
                index_offset = 0

    entries: list[_Xp3Entry] = []
    unknown_chunks: list[str] = []
    for tag, body in _iter_xp3_chunks(b"".join(chunks)):
        if tag != b"File":
            unknown_chunks.append(tag.decode("latin-1", errors="replace"))
            continue
        entry = _parse_xp3_file_entry(body)
        if entry is not None:
            entries.append(entry)
    return _Xp3Index(tuple(entries), tuple(unknown_chunks), chained)


def _parse_xp3_file_entry(body: bytes) -> _Xp3Entry | None:
    name = ""
    original_size = 0
    stored_size = 0
    adler: int | None = None
    segments: list[_Xp3Segment] = []
    for tag, chunk in _iter_xp3_chunks(body):
        if tag == b"info":
            if len(chunk) < 22:
                continue
            _flags, original_size, stored_size, name_len = struct.unpack_from("<IQQH", chunk, 0)
            name_bytes = chunk[22:22 + name_len * 2]
            name = name_bytes.decode("utf-16le", errors="replace").replace("\\", "/")
        elif tag == b"segm":
            offset = 0
            while offset + 28 <= len(chunk):
                flags = struct.unpack_from("<I", chunk, offset)[0]
                data_offset, original, stored = struct.unpack_from("<QQQ", chunk, offset + 4)
                segments.append(_Xp3Segment(flags, data_offset, original, stored))
                offset += 28
        elif tag == b"adlr":
            if len(chunk) >= 4:
                adler = struct.unpack_from("<I", chunk, 0)[0]
    if not name or not segments:
        return None
    return _Xp3Entry(name=name, original_size=original_size, stored_size=stored_size, segments=tuple(segments), adler=adler)


def _iter_xp3_chunks(data: bytes):
    cursor = 0
    while cursor + 12 <= len(data):
        tag = data[cursor:cursor + 4]
        size = struct.unpack_from("<Q", data, cursor + 4)[0]
        end = cursor + 12 + size
        if end > len(data):
            break
        yield tag, data[cursor + 12:end]
        cursor = end


def _read_xp3_entry_bytes(path: Path, entry: _Xp3Entry) -> bytes:
    parts: list[bytes] = []
    with path.open("rb") as f:
        for segment in entry.segments:
            f.seek(segment.offset)
            data = f.read(segment.stored_size)
            if len(data) != segment.stored_size:
                raise ValueError("truncated XP3 segment")
            if segment.flags & 1:
                data = zlib.decompress(data)
            if len(data) != segment.original_size:
                raise ValueError("XP3 segment size mismatch")
            parts.append(data)
    return b"".join(parts)


def _read_u64(f) -> int:
    data = f.read(8)
    if len(data) != 8:
        raise ValueError("unexpected EOF")
    return struct.unpack("<Q", data)[0]


def _maybe_compress(data: bytes) -> tuple[bytes, bool]:
    compressed = zlib.compress(data, level=9)
    if len(compressed) < len(data):
        return compressed, True
    return data, False


def _xp3_file_entry(
    rel: str,
    data: bytes,
    offset: int,
    stored_size: int,
    compressed: bool,
    *,
    adler_override: int | None = None,
) -> bytes:
    adler = int(adler_override) & 0xFFFFFFFF if adler_override is not None else zlib.adler32(data) & 0xFFFFFFFF
    filename = rel.replace("\\", "/")
    info_chunk = _xp3_info_chunk(filename, len(data), stored_size)
    segm_chunk = b"segm" + struct.pack("<Q", 28) + struct.pack("<BxxxQQQ", 1 if compressed else 0, offset, len(data), stored_size)
    adlr_chunk = b"adlr" + struct.pack("<QI", 4, adler)
    body = info_chunk + segm_chunk + adlr_chunk
    return b"File" + struct.pack("<Q", len(body)) + body


def _xp3_file_entry_from_segments(
    rel: str,
    original_size: int,
    stored_size: int,
    segments: tuple[_Xp3Segment, ...],
) -> bytes:
    filename = rel.replace("\\", "/")
    info_chunk = _xp3_info_chunk(filename, original_size, stored_size)
    segment_bytes = b"".join(
        struct.pack(
            "<BxxxQQQ",
            segment.flags & 0xFF,
            segment.offset,
            segment.original_size,
            segment.stored_size,
        )
        for segment in segments
    )
    segm_chunk = b"segm" + struct.pack("<Q", len(segment_bytes)) + segment_bytes
    body = info_chunk + segm_chunk
    return b"File" + struct.pack("<Q", len(body)) + body


def _xp3_info_chunk(filename: str, original_size: int, stored_size: int) -> bytes:
    name_bytes = filename.encode("utf-16le")
    size = 4 + 8 + 8 + 2 + len(name_bytes)
    return (
        b"info"
        + struct.pack("<QIQQH", size, 0, original_size, stored_size, len(filename))
        + name_bytes
    )


def _xp3_entry_is_script(entry: _Xp3Entry) -> bool:
    suffix = Path(entry.name).suffix.lower()
    return suffix in {".ks", ".tjs", ".scn"}


def _xp3_entry_is_obfuscated_script_candidate(xp3_path: Path, entry: _Xp3Entry) -> bool:
    if Path(entry.name).suffix:
        return False
    if entry.original_size <= 0 or entry.original_size > 8 * 1024 * 1024:
        return False
    if not _is_script_archive_name(xp3_path.name):
        return False
    # PackinOne/Yuzu-style protected KiriKiri games can hide scenario names
    # behind extensionless resource ids. Plain warning/readme entries are not
    # useful dump targets, so only include entries that are not statically
    # readable as normal text.
    try:
        data = _read_xp3_entry_bytes(xp3_path, entry)
    except Exception:
        return True
    if is_kirikiri_scn(data):
        return True
    return _decode_script_bytes_for_quality(data) is None


def _xp3_entry_needs_runtime_dump(xp3_path: Path, entry: _Xp3Entry) -> bool:
    if entry.original_size <= 0 or entry.original_size > 8 * 1024 * 1024:
        return False
    if _xp3_entry_is_obfuscated_script_candidate(xp3_path, entry):
        return True
    if not _xp3_entry_is_script(entry):
        return False
    try:
        data = _read_xp3_entry_bytes(xp3_path, entry)
    except Exception:
        return True
    if entry.name.lower().endswith(".scn"):
        return not is_kirikiri_scn(data)
    if _is_probably_binary_script(data):
        return False
    return _is_probably_protected_text_script(data)


def _xp3_entry_is_lightweight_obfuscated_script_candidate(xp3_path: Path, entry: _Xp3Entry) -> bool:
    if Path(entry.name).suffix:
        return False
    if entry.original_size <= 0 or entry.original_size > 8 * 1024 * 1024:
        return False
    return _is_script_archive_name(xp3_path.name)


def _is_script_archive_name(name: str) -> bool:
    lower = name.lower()
    stem = Path(lower).stem
    return (
        stem in {"scn", "scenario", "script", "scripts", "scenario_adv", "scenario_common"}
        or "scenario" in stem
        or stem.endswith("_scn")
        or stem.endswith("scn")
        or stem.startswith("scn")
        or "script" in stem
    )


def _select_script_xp3_files(xp3_files: list[Path]) -> list[Path]:
    scored: list[tuple[int, int, Path]] = []
    fallback: list[Path] = []
    for path in xp3_files:
        name = path.name.lower()
        if _is_generated_kirikiri_patch_archive(path):
            continue
        if any(token in name for token in ("voice", "bgimage", "fgimage", "evimage", "video", "movie", "sound", "music", "bgm", "se")):
            fallback.append(path)
            continue
        score = 0
        script_count = 0
        if _is_script_archive_name(path.name):
            score += 100
        if Path(name).stem in {"data", "main", "system", "startup", "config"}:
            score += 100
        elif "patch" in name:
            score += 20
        try:
            index = _read_xp3_index(path)
        except Exception:
            scored.append((score, script_count, path))
            continue
        for entry in index.entries:
            if Path(entry.name).name.lower().startswith("startup"):
                continue
            if _xp3_entry_is_script(entry):
                score += 10
                script_count += 1
            elif _xp3_entry_is_lightweight_obfuscated_script_candidate(path, entry):
                score += 8
                script_count += 1
        scored.append((score, script_count, path))

    selected = [path for score, script_count, path in sorted(scored, key=lambda row: (-row[0], row[2].name.lower())) if score > 0 and (script_count > 0 or score >= 100)]
    remaining = [path for path in xp3_files if path not in fallback and not _is_generated_kirikiri_patch_archive(path)]
    return selected or remaining or fallback[:1]


def _is_generated_kirikiri_patch_archive(path: Path) -> bool:
    name = path.name.lower()
    if name in {"patch.xp3", "kirikiri_patch.xp3"}:
        return True
    return any(part.lower() == "_translation_meta" for part in path.parts)
