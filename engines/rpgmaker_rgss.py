"""Static RGSS extraction and verified deployment, separate from MV/MZ hooks."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from engines.base import TextItem
from utils.logger import info
from utils.rgss_archive import (
    extract_rgss_archive, read_rgss_archive, rebuild_rgss_archive, safe_relative_name,
)
from utils.rvdata2 import (
    extract_strings_from_marshal,
    replace_any_strings_in_marshal_full,
    replace_strings_in_marshal_full,
)
from utils.rgss_scripts import extract_rgss_script_literals, replace_rgss_script_literals
from utils.rgss_events import extract_rgss_event_strings


DATA_SUFFIXES = frozenset({".rxdata", ".rvdata", ".rvdata2"})
ARCHIVE_SUFFIXES = frozenset({".rgssad", ".rgss2a", ".rgss3a"})


def find_archives(game_dir: Path) -> list[Path]:
    return sorted(
        (path for path in game_dir.iterdir() if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES),
        key=lambda path: path.name.casefold(),
    )


def find_data_files(game_dir: Path) -> list[Path]:
    data_dir = next((path for path in game_dir.iterdir()
                     if path.is_dir() and path.name.casefold() == "data"), None)
    return sorted((path for path in data_dir.rglob("*")
                   if path.is_file() and path.suffix.lower() in DATA_SUFFIXES)) if data_dir else []


def _safe_target(root: Path, relative: str) -> Path:
    target = root / safe_relative_name(relative)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"RGSS target escapes game/workspace: {relative!r}")
    return target


def deploy_changes(game_dir: Path, changes: dict[Path, bytes], manifest=None) -> None:
    """Stage every output and create every backup before touching game files."""
    staged: list[tuple[Path, Path]] = []
    try:
        for target, data in changes.items():
            if not target.resolve().is_relative_to(game_dir.resolve()) or not target.is_file():
                raise ValueError(f"Invalid RGSS deployment target: {target}")
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".engaixt-rgss-", delete=False) as stream:
                temporary = Path(stream.name)
                staged.append((target, temporary))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for target, _ in staged:
            if manifest is not None:
                backup = manifest.backup_file(target)
                if backup is None or not backup.is_file():
                    raise OSError(f"Cannot back up RGSS game file: {target}")
            else:
                from core.workspace import safe_replace
                safe_replace(target)
        for target, temporary in staged:
            os.replace(temporary, target)
            if manifest is not None:
                manifest.record_modified(target)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)


class RGSSStatic:
    def __init__(self, game_dir: Path, workspace: Path):
        self.game_dir = game_dir
        self.original = workspace / "original"
        archives = find_archives(game_dir)
        if len(archives) > 1:
            raise ValueError("Multiple RGSS archives: cannot safely choose a data source")
        self.archive_path = archives[0] if archives else None
        self.archive_bytes = self.archive_path.read_bytes() if self.archive_path else None
        self.archive = read_rgss_archive(self.archive_bytes) if self.archive_bytes is not None else None
        self.verification: dict = {}

    def unpack(self) -> list[TextItem]:
        if self.archive is not None:
            extract_rgss_archive(self.archive, self.original)
            files = [
                self.original / entry.name for entry in self.archive.entries
                if entry.name.split("/", 1)[0].casefold() == "data"
                and Path(entry.name).suffix.lower() in DATA_SUFFIXES
            ]
            resources = {
                Path(entry.name).stem for entry in self.archive.entries
                if entry.name.split("/", 1)[0].casefold() in {"graphics", "audio"}
            }
        else:
            files = []
            resources = set()
            for directory in ("Graphics", "Audio"):
                root = self.game_dir / directory
                if root.is_dir():
                    resources.update(path.stem for path in root.rglob("*") if path.is_file())
            for source in find_data_files(self.game_dir):
                target = _safe_target(self.original, source.relative_to(self.game_dir).as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                files.append(target)

        items: list[TextItem] = []
        for path in files:
            data = path.read_bytes()
            # A no-op reserialization must preserve every byte before translation.
            if replace_strings_in_marshal_full(data, {}) != data:
                raise ValueError(f"Unsupported or lossy RGSS Marshal structure: {path.name}")
            relative = path.relative_to(self.original).as_posix()
            if Path(relative).stem.casefold() == "scripts":
                script_literals = extract_rgss_script_literals(data)
                for index, literal in enumerate(script_literals):
                    items.append(TextItem(
                        file=relative,
                        key=(f"script_{literal['script_index']:04d}_"
                             f"literal_{literal['ordinal']:04d}"),
                        original=literal["source"],
                        context=f"RGSS script: {literal['script_name']}" if literal["script_name"] else "RGSS script",
                        meta={
                            "source_kind": "rgss_marshal_script",
                            "rgss_archive": self.archive_path.name if self.archive_path else "",
                            **literal,
                        },
                    ))
            elif _is_event_data(relative):
                event_strings = extract_rgss_event_strings(data)
                event_originals = {event_text.original for event_text in event_strings}
                for index, event_text in enumerate(event_strings):
                    items.append(TextItem(
                        file=relative,
                        key=(f"event_{event_text.event_code:03d}_"
                             f"param_{'_'.join(str(part) for part in event_text.parameter_path)}_"
                             f"{index:04d}"),
                        original=event_text.original,
                        context=event_text.context,
                        meta={
                            "source_kind": "rgss_event_command",
                            "source_sha256": hashlib.sha256(data).hexdigest(),
                            "rgss_archive": self.archive_path.name if self.archive_path else "",
                            "event_code": event_text.event_code,
                            "parameter_path": list(event_text.parameter_path),
                        },
                    ))
                offset = len(event_strings)
                for index, text in enumerate(dict.fromkeys(extract_strings_from_marshal(data))):
                    if text in resources or text in event_originals:
                        continue
                    items.append(TextItem(
                        file=relative, key=f"str_{offset + index:04d}", original=text,
                        meta={
                            "source_kind": "rgss_marshal",
                            "source_sha256": hashlib.sha256(data).hexdigest(),
                            "rgss_archive": self.archive_path.name if self.archive_path else "",
                        },
                    ))
            else:
                for index, text in enumerate(dict.fromkeys(extract_strings_from_marshal(data))):
                    if text in resources:
                        continue
                    items.append(TextItem(
                        file=relative, key=f"str_{index:04d}", original=text,
                        meta={
                            "source_kind": "rgss_marshal",
                            "source_sha256": hashlib.sha256(data).hexdigest(),
                            "rgss_archive": self.archive_path.name if self.archive_path else "",
                        },
                    ))
        info(f"RGSS 静态提取完成: {len(items)} 条文本")
        return items

    def repack(self, items: list[TextItem], manifest=None) -> None:
        grouped: dict[str, list[TextItem]] = {}
        for item in items:
            if item.translated and item.translated != item.original:
                grouped.setdefault(safe_relative_name(item.file), []).append(item)
        replacements: dict[str, bytes] = {}
        hits = 0
        entries = {entry.name: entry.data for entry in self.archive.entries} if self.archive else {}
        for relative, file_items in grouped.items():
            target = _safe_target(self.original, relative)
            if (
                relative.split("/", 1)[0].casefold() != "data"
                or Path(relative).suffix.lower() not in DATA_SUFFIXES
                or not target.is_file()
            ):
                raise ValueError(f"Missing RGSS repack source: {relative}")
            data = target.read_bytes()
            current = entries.get(relative) if self.archive else _safe_target(self.game_dir, relative).read_bytes()
            if current != data:
                raise ValueError(f"RGSS source changed since extraction: {relative}")
            if replace_strings_in_marshal_full(data, {}) != data:
                raise ValueError(f"Unsupported RGSS Marshal structure: {relative}")
            kinds = {item.meta.get("source_kind") for item in file_items}
            is_script = kinds == {"rgss_marshal_script"}
            if "rgss_marshal_script" in kinds and not is_script:
                raise ValueError(f"RGSS script and ordinary Marshal items are mixed: {relative}")
            for item in file_items:
                expected_archive = item.meta.get("rgss_archive")
                if expected_archive and (
                    self.archive_path is None or expected_archive != self.archive_path.name
                ):
                    raise ValueError(f"RGSS checkpoint archive no longer matches: {relative}")
                expected = (
                    item.meta.get("marshal_source_sha256")
                    if is_script else item.meta.get("source_sha256")
                )
                if expected and expected != hashlib.sha256(data).hexdigest():
                    raise ValueError(f"RGSS checkpoint source hash changed: {relative}")
            if is_script:
                script_updates = [
                    {
                        "script_index": item.meta.get("script_index"),
                        "span_start": item.meta.get("span_start"),
                        "span_end": item.meta.get("span_end"),
                        "original": item.original,
                        "translated": item.translated,
                        "source_sha256": item.meta.get("script_source_sha256"),
                    }
                    for item in file_items
                ]
                output = replace_rgss_script_literals(data, script_updates)
                translations = {
                    item.original: item.translated
                    for item in file_items
                    if item.translated and item.translated != item.original
                }
            else:
                available = set(extract_strings_from_marshal(data))
                is_event_file = _is_event_data(relative)
                if is_event_file:
                    available.update(
                        event_text.original
                        for event_text in extract_rgss_event_strings(data)
                    )
                translations = {item.original: item.translated for item in file_items}
                if not translations.keys() <= available:
                    raise ValueError(f"RGSS checkpoint source text no longer matches: {relative}")
                output = (
                    replace_any_strings_in_marshal_full(data, translations)
                    if is_event_file
                    else replace_strings_in_marshal_full(data, translations)
                )
            if replace_strings_in_marshal_full(output, {}) != output:
                raise ValueError(f"RGSS rewritten data cannot be reparsed: {relative}")
            if is_script:
                expected = [item.translated for item in file_items if item.translated and item.translated != item.original]
                actual = [literal["source"] for literal in extract_rgss_script_literals(output)]
                if not all(text in actual for text in expected):
                    raise ValueError(f"RGSS translated script literals missing after serialization: {relative}")
            elif any(text.encode("utf-8") not in output for text in translations.values()):
                raise ValueError(f"RGSS translated strings missing after serialization: {relative}")
            if output != data:
                replacements[relative] = output
                hits += len(translations)

        if not replacements:
            return
        if self.archive is not None:
            if self.archive_path.read_bytes() != self.archive_bytes:
                raise ValueError("RGSS archive changed during repack")
            output = rebuild_rgss_archive(self.archive, replacements)
            verified = read_rgss_archive(output)
            if [entry.raw_name for entry in verified.entries] != [entry.raw_name for entry in self.archive.entries]:
                raise ValueError("RGSS rebuilt archive entry order/names changed")
            if any(entry.data != replacements.get(entry.name, entries[entry.name]) for entry in verified.entries):
                raise ValueError("RGSS rebuilt archive roundtrip failed")
            changes = {self.archive_path: output}
        else:
            changes = {_safe_target(self.game_dir, relative): data for relative, data in replacements.items()}
        deploy_changes(self.game_dir, changes, manifest)
        self.verification.update({
            "checked": True, "hits": hits, "files_checked": len(changes),
            "archive_roundtrip_verified": self.archive is not None,
            "archive_version": self.archive.version if self.archive else None,
            "outputs": [str(target) for target in changes],
        })
        info(f"RGSS 静态回填完成: {hits} 条译文，{len(changes)} 个实际游戏文件")


def _is_event_data(relative: str) -> bool:
    name = Path(relative).name.casefold()
    return name.startswith("map") or name.startswith("commonevents")
