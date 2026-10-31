"""Archive support for TyranoScript games packaged by NW.js or Electron."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, is_zipfile


_REQUIRED_MARKERS = {
    "index.html",
    "package.json",
    "tyrano/plugins/kag/kag.tag_system.js",
}
_MAX_ASAR_HEADER_SIZE = 128 * 1024 * 1024


@dataclass(frozen=True)
class _AsarIndex:
    header: dict
    entries: dict[str, tuple[str, dict]]
    data_start: int
    file_size: int


def find_packed_tyrano_exe(game_dir: Path) -> Path | None:
    candidates = sorted(
        game_dir.glob("*.exe"),
        key=lambda value: value.stat().st_size,
        reverse=True,
    )[:5]
    for candidate in candidates:
        if is_packed_tyrano_exe(candidate):
            return candidate
    return None


def is_packed_tyrano_exe(candidate: Path) -> bool:
    """Check the selected executable itself, not another executable beside it."""
    try:
        if not candidate.is_file() or candidate.stat().st_size < 1024 * 1024:
            return False
    except OSError:
        return False
    return is_tyrano_archive(candidate)


def is_tyrano_archive(candidate: Path) -> bool:
    """Validate Tyrano markers in a ZIP package or an Electron ASAR."""
    try:
        if not candidate.is_file():
            return False
        if is_zipfile(candidate):
            with ZipFile(candidate) as archive:
                normalized = {_norm(info.filename) for info in archive.infolist()}
            has_scenario = any(
                name.startswith("data/scenario/") and name.endswith(".ks")
                for name in normalized
            )
        else:
            entries = _read_asar_index(candidate).entries
            normalized = set(entries)
            has_scenario = any(
                name.startswith("data/scenario/")
                and name.endswith(".ks")
                and not entry.get("unpacked")
                for name, (_stored_name, entry) in entries.items()
            )
    except (BadZipFile, OSError, ValueError, json.JSONDecodeError, struct.error):
        return False
    return _REQUIRED_MARKERS <= normalized and has_scenario


def scenario_entry_names(executable: Path) -> list[str]:
    if is_zipfile(executable):
        with ZipFile(executable) as archive:
            names = [
                info.filename
                for info in archive.infolist()
                if _norm(info.filename).startswith("data/scenario/")
                and _norm(info.filename).endswith(".ks")
                and not info.is_dir()
                and _is_safe_entry_name(info.filename)
            ]
    else:
        index = _read_asar_index(executable)
        names = [
            name
            for normalized, (name, entry) in index.entries.items()
            if normalized.startswith("data/scenario/")
            and normalized.endswith(".ks")
            and not entry.get("unpacked")
        ]
    return sorted(names, key=str.casefold)


def read_archive_entries(executable: Path, names: list[str]) -> dict[str, bytes]:
    if is_zipfile(executable):
        with ZipFile(executable) as archive:
            by_name = {_norm(info.filename): info for info in archive.infolist()}
            result: dict[str, bytes] = {}
            for name in names:
                info = by_name.get(_norm(name))
                if info is not None:
                    result[name] = archive.read(info)
            return result

    index = _read_asar_index(executable)
    result = {}
    with executable.open("rb") as stream:
        for name in names:
            match = index.entries.get(_norm(name))
            if match is None:
                continue
            _stored_name, entry = match
            if entry.get("unpacked"):
                continue
            offset, size = _asar_extent(entry, index, name)
            stream.seek(index.data_start + offset)
            data = stream.read(size)
            if len(data) != size:
                raise ValueError(f"Truncated ASAR entry: {name}")
            result[name] = data
    return result


def patch_embedded_asar(
    source_archive: Path,
    output_archive: Path,
    replacements: dict[str, Path],
) -> dict[str, int | bool]:
    """Append replacement entries and rebuild an Electron ASAR header."""
    index = _read_asar_index(source_archive)
    replacement_map = {_norm(name): (name, path) for name, path in replacements.items()}
    if len(replacement_map) != len(replacements):
        raise ValueError("Duplicate Tyrano ASAR replacement path")
    missing = sorted(set(replacement_map) - set(index.entries))
    if missing:
        raise KeyError(f"Tyrano ASAR entries not found: {', '.join(missing[:5])}")

    payload_size = index.file_size - index.data_start
    next_offset = payload_size
    for normalized, (_name, staged_path) in replacement_map.items():
        stored_name, entry = index.entries[normalized]
        if entry.get("unpacked") or "link" in entry:
            raise ValueError(f"Cannot replace unpacked or linked ASAR entry: {stored_name}")
        _asar_extent(entry, index, stored_name)
        size = staged_path.stat().st_size
        entry["offset"] = str(next_offset)
        entry["size"] = size
        _update_asar_integrity(entry, staged_path)
        next_offset += size

    header_bytes = _encode_asar_header(index.header)
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    if source_archive.resolve() == output_archive.resolve():
        raise ValueError("Tyrano ASAR output must be staged separately")
    try:
        with source_archive.open("rb") as source, output_archive.open("wb") as output:
            output.write(header_bytes)
            source.seek(index.data_start)
            shutil.copyfileobj(source, output, length=1024 * 1024)
            for _name, staged_path in replacement_map.values():
                with staged_path.open("rb") as staged:
                    shutil.copyfileobj(staged, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(source_archive, output_archive)
    except Exception:
        output_archive.unlink(missing_ok=True)
        raise

    verified = read_archive_entries(output_archive, list(replacements)) == {
        name: path.read_bytes() for name, path in replacements.items()
    }
    return {
        "replaced": len(replacements),
        "verified": verified,
        "output_size": output_archive.stat().st_size,
    }


def patch_embedded_zip(
    source_executable: Path,
    output_executable: Path,
    replacements: dict[str, Path],
) -> dict[str, int | bool]:
    """Copy an SFX executable and replace selected ZIP entries without recompressing assets."""
    output_executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_executable, output_executable)
    replacement_map = {_norm(name): path for name, path in replacements.items()}
    replaced = 0

    with output_executable.open("r+b") as file_obj:
        archive = ZipFile(file_obj, mode="a", allowZip64=True)
        try:
            original_infos = list(archive.infolist())
            matched_infos = {
                _norm(info.filename): info
                for info in original_infos
                if _norm(info.filename) in replacement_map
            }
            missing = sorted(set(replacement_map) - set(matched_infos))
            if missing:
                raise KeyError(f"Tyrano embedded entries not found: {', '.join(missing[:5])}")

            # Append mode starts writing at the old central directory. Removing
            # replaced entries from the new directory avoids duplicate names;
            # their old local payloads remain unreferenced and harmless.
            archive.filelist = [
                info for info in original_infos if _norm(info.filename) not in replacement_map
            ]
            archive.NameToInfo = {info.filename: info for info in archive.filelist}

            for normalized, path in replacement_map.items():
                original = matched_infos[normalized]
                replacement_info = copy.copy(original)
                replacement_info.CRC = 0
                replacement_info.compress_size = 0
                replacement_info.file_size = 0
                archive.writestr(replacement_info, path.read_bytes())
                replaced += 1
        finally:
            archive.close()
        # A shorter central directory must not leave the old EOCD after the new one.
        file_obj.truncate(file_obj.tell())

    verified = verify_embedded_replacements(output_executable, replacements)
    return {
        "replaced": replaced,
        "verified": verified,
        "output_size": output_executable.stat().st_size,
    }


def verify_embedded_replacements(executable: Path, replacements: dict[str, Path]) -> bool:
    with ZipFile(executable) as archive:
        by_name = {_norm(info.filename): info for info in archive.infolist()}
        for name, path in replacements.items():
            info = by_name.get(_norm(name))
            if info is None or archive.read(info) != path.read_bytes():
                return False
    return True


def _norm(value: str) -> str:
    return value.replace("\\", "/").lstrip("/").casefold()


def _read_asar_index(path: Path) -> _AsarIndex:
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        prefix = stream.read(16)
        if len(prefix) != 16 or struct.unpack_from("<I", prefix, 0)[0] != 4:
            raise ValueError("Not an Electron ASAR archive")
        header_size = struct.unpack_from("<I", prefix, 4)[0]
        pickle_size = struct.unpack_from("<I", prefix, 8)[0]
        json_size = struct.unpack_from("<I", prefix, 12)[0]
        if (
            header_size < 8
            or header_size > _MAX_ASAR_HEADER_SIZE
            or header_size > file_size - 8
            or pickle_size != header_size - 4
            or json_size > header_size - 8
            or header_size - 8 - json_size > 3
        ):
            raise ValueError("Invalid Electron ASAR header size")
        raw_header = stream.read(json_size)
    header = json.loads(raw_header.decode("utf-8"))
    if not isinstance(header, dict) or not isinstance(header.get("files"), dict):
        raise ValueError("Invalid Electron ASAR file tree")

    entries: dict[str, tuple[str, dict]] = {}

    stack = [(header["files"], "")]
    while stack:
        directory, prefix = stack.pop()
        for name, value in directory.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError("Invalid Electron ASAR directory entry")
            rel = f"{prefix}/{name}" if prefix else name
            if not _is_safe_entry_name(rel):
                raise ValueError(f"Unsafe Electron ASAR entry: {rel}")
            children = value.get("files")
            if isinstance(children, dict):
                stack.append((children, rel))
            elif "link" not in value:
                key = _norm(rel)
                if key in entries:
                    raise ValueError(f"Duplicate Electron ASAR entry: {rel}")
                entries[key] = (rel, value)

    return _AsarIndex(header, entries, 8 + header_size, file_size)


def _asar_extent(entry: dict, index: _AsarIndex, name: str) -> tuple[int, int]:
    try:
        offset = int(entry["offset"])
        size = int(entry["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ASAR entry extent: {name}") from exc
    if offset < 0 or size < 0 or offset + size > index.file_size - index.data_start:
        raise ValueError(f"ASAR entry is outside the archive: {name}")
    return offset, size


def _encode_asar_header(header: dict) -> bytes:
    raw_header = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padding = (-len(raw_header)) % 4
    header_size = 8 + len(raw_header) + padding
    if header_size > 0xFFFFFFFF:
        raise ValueError("Electron ASAR header is too large")
    return b"".join((
        struct.pack("<I", 4),
        struct.pack("<I", header_size),
        struct.pack("<I", header_size - 4),
        struct.pack("<I", len(raw_header)),
        raw_header,
        b"\0" * padding,
    ))


def _update_asar_integrity(entry: dict, path: Path) -> None:
    integrity = entry.get("integrity")
    if integrity is None:
        return
    if not isinstance(integrity, dict) or str(integrity.get("algorithm", "")).casefold() != "sha256":
        raise ValueError("Unsupported ASAR integrity metadata")
    block_size = int(integrity.get("blockSize") or 4 * 1024 * 1024)
    if block_size <= 0:
        raise ValueError("Invalid ASAR integrity block size")

    digest = hashlib.sha256()
    blocks = []
    with path.open("rb") as stream:
        while True:
            block = stream.read(block_size)
            if not block:
                break
            digest.update(block)
            blocks.append(hashlib.sha256(block).hexdigest())
    integrity["hash"] = digest.hexdigest()
    integrity["blockSize"] = block_size
    integrity["blocks"] = blocks


def _is_safe_entry_name(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        bool(path.parts) and not path.is_absolute()
        and ".." not in path.parts
        and all(":" not in part for part in path.parts)
    )
