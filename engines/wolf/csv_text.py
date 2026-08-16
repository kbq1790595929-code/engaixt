from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from engines.base import TextItem
from utils.text_extract import is_translatable


_TEXT_COLUMN = 8
_NEWLINE = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class _Cell:
    row: int
    column: int
    start: int
    end: int
    value: str
    raw: str


@dataclass(frozen=True)
class _CsvDocument:
    text: str
    encoding: str
    newline: str
    cells: tuple[_Cell, ...]

    def find_cell(self, row: int, column: int) -> _Cell | None:
        for cell in self.cells:
            if cell.row == row and cell.column == column:
                return cell
        return None


def extract_script_text_items(
    data_root: Path,
    target_resolver: Callable[[Path], tuple[str, str]],
) -> list[TextItem]:
    """Extract dialogue held in WOLF game's Script/*.csv files.

    The script format is a normal CSV table with dialogue stored in the ninth
    column. We retain the original cell span, encoding and line endings so
    patching changes only the translated cell, including quoted multiline
    dialogue.
    """
    script_root = data_root / "Script"
    if not script_root.is_dir():
        return []

    items: list[TextItem] = []
    for path in sorted(script_root.rglob("*.csv"), key=lambda value: str(value).casefold()):
        document = _read_document(path)
        source_hash = _sha256(path)
        csv_rel = path.relative_to(data_root).as_posix()
        target_file, source_binary = target_resolver(path)
        for cell in document.cells:
            if cell.row == 0 or cell.column != _TEXT_COLUMN:
                continue
            original = _normalise_newlines(cell.value)
            if not original or not is_translatable(original.replace("\n", " ")):
                continue
            items.append(TextItem(
                file=target_file,
                key=f"{csv_rel}:{cell.row}:{cell.column}",
                original=original,
                context=f"WOLF Script / {csv_rel} / row {cell.row + 1}",
                line=cell.row + 1,
                meta={
                    "wolf_csv_rel": csv_rel,
                    "wolf_csv_row": cell.row,
                    "wolf_csv_column": cell.column,
                    "wolf_csv_sha256": source_hash,
                    "wolf_csv_encoding": document.encoding,
                    "wolf_csv_newline": document.newline,
                    "wolf_csv_cell_newline": _first_newline(cell.value) or document.newline,
                    "wolf_source_binary": source_binary,
                    "wolf_role": "script_dialogue",
                },
            ))
    return items


def apply_script_translations(data_root: Path, generated_root: Path, items: list[TextItem]) -> int:
    """Write translated Script CSV files to the generated patch tree only."""
    grouped: dict[str, list[TextItem]] = {}
    for item in items:
        meta = item.meta or {}
        csv_rel = str(meta.get("wolf_csv_rel") or "")
        if csv_rel and item.translated and item.translated != item.original:
            grouped.setdefault(csv_rel, []).append(item)

    changed = 0
    root = data_root.resolve()
    for csv_rel, file_items in grouped.items():
        source = (data_root / csv_rel).resolve()
        if root not in source.parents or source.suffix.casefold() != ".csv":
            raise ValueError(f"WOLF CSV path escapes data root: {csv_rel}")
        if not source.is_file():
            raise ValueError(f"WOLF CSV source is missing: {csv_rel}")

        expected_hashes = {str(item.meta.get("wolf_csv_sha256") or "") for item in file_items}
        if len(expected_hashes) != 1 or not next(iter(expected_hashes)):
            raise ValueError(f"WOLF CSV source hash is inconsistent: {csv_rel}")
        if _sha256(source) != next(iter(expected_hashes)):
            raise ValueError(f"WOLF CSV source changed before repack: {csv_rel}")

        document = _read_document(source)
        replacements: list[tuple[int, int, str]] = []
        seen_locations: set[tuple[int, int]] = set()
        for item in file_items:
            meta = item.meta
            row = int(meta.get("wolf_csv_row", -1))
            column = int(meta.get("wolf_csv_column", -1))
            location = (row, column)
            if location in seen_locations:
                raise ValueError(f"WOLF CSV has duplicate patch target: {csv_rel} row {row + 1}")
            seen_locations.add(location)
            cell = document.find_cell(row, column)
            if cell is None:
                raise ValueError(f"WOLF CSV patch target is missing: {csv_rel} row {row + 1}")
            if _normalise_newlines(cell.value) != item.original:
                raise ValueError(f"WOLF CSV source changed before repack: {csv_rel} row {row + 1}")
            newline = str(meta.get("wolf_csv_cell_newline") or document.newline)
            translated = _normalise_newlines(item.translated).replace("\n", newline)
            replacements.append((cell.start, cell.end, _encode_cell(translated, cell.raw)))

        patched = document.text
        for start, end, replacement in sorted(replacements, reverse=True):
            patched = patched[:start] + replacement + patched[end:]
        try:
            output = patched.encode(document.encoding)
        except UnicodeEncodeError as exc:
            raise RuntimeError(
                f"WOLF CSV {csv_rel} uses {document.encoding}; it cannot store Chinese text without a Unicode game build"
            ) from exc

        destination = generated_root / csv_rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(output)
        changed += len(replacements)
    return changed


def _read_document(path: Path) -> _CsvDocument:
    raw = path.read_bytes()
    encoding = _detect_encoding(raw)
    text = raw.decode(encoding)
    return _CsvDocument(
        text=text,
        encoding=encoding,
        newline=_first_newline(text) or "\r\n",
        cells=tuple(_parse_cells(text)),
    )


def _detect_encoding(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return "utf-16"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp932"


def _parse_cells(text: str) -> list[_Cell]:
    cells: list[_Cell] = []
    length = len(text)
    position = 0
    row = 0
    column = 0

    while position < length:
        start = position
        if text[position] == '"':
            position += 1
            value_parts: list[str] = []
            while position < length:
                current = text[position]
                if current != '"':
                    value_parts.append(current)
                    position += 1
                    continue
                if position + 1 < length and text[position + 1] == '"':
                    value_parts.append('"')
                    position += 2
                    continue
                position += 1
                break
            else:
                raise ValueError(f"Invalid WOLF CSV: unterminated quoted cell in row {row + 1}")
            end = position
            value = "".join(value_parts)
            if position < length and text[position] not in ",\r\n":
                raise ValueError(f"Invalid WOLF CSV: unexpected data after quoted cell in row {row + 1}")
        else:
            while position < length and text[position] not in ",\r\n":
                position += 1
            end = position
            value = text[start:end]

        cells.append(_Cell(row, column, start, end, value, text[start:end]))
        column += 1
        if position == length:
            break
        if text[position] == ",":
            position += 1
            continue
        if text[position] == "\r" and position + 1 < length and text[position + 1] == "\n":
            position += 2
        else:
            position += 1
        row += 1
        column = 0

    return cells


def _encode_cell(value: str, original_raw: str) -> str:
    needs_quotes = original_raw.startswith('"') or any(character in value for character in ',"\r\n')
    if not needs_quotes:
        return value
    return '"' + value.replace('"', '""') + '"'


def _normalise_newlines(value: str) -> str:
    return _NEWLINE.sub("\n", value)


def _first_newline(value: str) -> str | None:
    match = _NEWLINE.search(value)
    return match.group(0) if match else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
