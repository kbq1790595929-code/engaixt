"""Embedded ZIP support for NW.js executables used by TyranoScript games."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, is_zipfile


_REQUIRED_MARKERS = {
    "index.html",
    "package.json",
    "tyrano/plugins/kag/kag.tag_system.js",
}


def find_packed_tyrano_exe(game_dir: Path) -> Path | None:
    candidates = sorted(
        game_dir.glob("*.exe"),
        key=lambda value: value.stat().st_size,
        reverse=True,
    )[:5]
    for candidate in candidates:
        if candidate.stat().st_size < 1024 * 1024 or not is_zipfile(candidate):
            continue
        try:
            with ZipFile(candidate) as archive:
                normalized = {_norm(info.filename) for info in archive.infolist()}
        except (BadZipFile, OSError):
            continue
        if _REQUIRED_MARKERS <= normalized and any(
            name.startswith("data/scenario/") and name.endswith(".ks")
            for name in normalized
        ):
            return candidate
    return None


def scenario_entry_names(executable: Path) -> list[str]:
    with ZipFile(executable) as archive:
        return sorted(
            (
                info.filename
                for info in archive.infolist()
                if _norm(info.filename).startswith("data/scenario/")
                and _norm(info.filename).endswith(".ks")
                and not info.is_dir()
                and _is_safe_entry_name(info.filename)
            ),
            key=str.casefold,
        )


def read_archive_entries(executable: Path, names: list[str]) -> dict[str, bytes]:
    with ZipFile(executable) as archive:
        by_name = {_norm(info.filename): info for info in archive.infolist()}
        result: dict[str, bytes] = {}
        for name in names:
            info = by_name.get(_norm(name))
            if info is not None:
                result[name] = archive.read(info)
        return result


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


def _is_safe_entry_name(value: str) -> bool:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and all(":" not in part for part in path.parts)
    )
