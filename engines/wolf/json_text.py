from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from engines.base import TextItem
from engines.wolf.text_filter import classify_database_value, classify_wolf_text
from utils.text_extract import is_translatable


_NORMAL_COMMANDS = {"Message", "Choices", "SetString"}
_LEADING_BREAKS = re.compile(r"^[\r\n]+")
_TRAILING_BREAKS = re.compile(r"[\r\n]+$")
_MESSAGE_DIRECTIVE = re.compile(r"^(?:@[0-9]+(?:\r\r|\r\n|\r|\n))+")


def extract_text_items(
    json_root: Path,
    target_resolver: Callable[[str], tuple[str, str]],
    filter_stats: dict[str, int] | None = None,
) -> list[TextItem]:
    items: list[TextItem] = []
    for path in sorted(json_root.rglob("*.json"), key=lambda p: str(p).casefold()):
        rel = path.relative_to(json_root).as_posix()
        data = json.loads(path.read_text(encoding="utf-8"))
        target_file, source_binary = target_resolver(rel)
        kind = rel.split("/", 1)[0]
        if kind == "game":
            _extract_game(data, rel, target_file, source_binary, items, filter_stats)
        elif kind == "databases":
            _extract_database(data, rel, target_file, source_binary, items, filter_stats)
        else:
            _extract_commands(data, [], rel, target_file, source_binary, items, filter_stats)
    return items


def apply_translations(json_root: Path, items: list[TextItem]) -> int:
    grouped: dict[str, list[TextItem]] = {}
    for item in items:
        meta = item.meta or {}
        rel = str(meta.get("wolf_json") or "")
        if rel and item.translated and item.translated != item.original:
            grouped.setdefault(rel, []).append(item)

    changed = 0
    root = json_root.resolve()
    for rel, file_items in grouped.items():
        path = (json_root / rel).resolve()
        if root not in path.parents:
            raise ValueError(f"WOLF JSON path escapes workspace: {rel}")
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in file_items:
            pointer = str(item.meta.get("wolf_pointer") or "")
            current = _get_pointer(data, pointer)
            if not isinstance(current, str):
                raise ValueError(f"WOLF patch target is not text: {rel} {pointer}")
            # Custom databases can mirror a record name into a string field.
            # Common events use that value as a lookup key, so old checkpoints
            # must not be allowed to translate it as an ordinary value.
            if _is_database_record_alias(data, pointer):
                continue
            normalized, _, _, _ = _normalize_text(current)
            if normalized != item.original:
                raise ValueError(f"WOLF source changed before repack: {rel} {pointer}")
            eol = str(item.meta.get("wolf_eol") or "\r\r")
            prefix = str(item.meta.get("wolf_prefix") or "")
            suffix = str(item.meta.get("wolf_suffix") or "")
            translated = item.translated.replace("\r\n", "\n").replace("\r", "\n")
            _set_pointer(data, pointer, prefix + translated.replace("\n", eol) + suffix)
            changed += 1
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        temp.replace(path)
    return changed


def _extract_game(data, rel, target, source, items, filter_stats):
    # WOLF stores the game title in save metadata and uses it as an identity
    # check. Translating Title/TitlePlus makes existing saves look foreign.
    for key in ("Title", "TitlePlus"):
        if isinstance(data.get(key), str) and _is_translation_candidate(data[key]):
            _record_filter_skip(filter_stats, "game_identity")
    for key in ("StartUpMsg", "TitleMsg"):
        if isinstance(data.get(key), str):
            _append_item(items, data[key], [key], rel, target, source, "game", filter_stats=filter_stats)


def _extract_database(data, rel, target, source, items, filter_stats):
    # Record names in every WOLF database are runtime lookup keys. Common
    # events can address DataBase/SysDatabase/CDataBase records by name, so a
    # translated record name leaves a structurally valid archive that fails
    # later with "DB data name does not exist" at runtime.
    for type_index, db_type in enumerate(data.get("types", [])):
        if not isinstance(db_type, dict):
            continue
        for data_index, record in enumerate(db_type.get("data", [])):
            if not isinstance(record, dict):
                continue
            record_name = record.get("name") if isinstance(record.get("name"), str) else ""
            if record_name and _is_translation_candidate(record_name):
                _record_filter_skip(filter_stats, "database_record_name")
            for field_index, field in enumerate(record.get("data", [])):
                if not isinstance(field, dict) or not isinstance(field.get("value"), str):
                    continue
                field_name = str(field.get("name") or "")
                field_value = field["value"]
                if not field_value.strip():
                    continue
                if record_name and field_value == record_name:
                    if _is_translation_candidate(field_value):
                        _record_filter_skip(filter_stats, "database_record_alias")
                    continue
                skip_reason = classify_database_value(field_name, field_value)
                if skip_reason:
                    if _is_translation_candidate(field_value):
                        _record_filter_skip(filter_stats, skip_reason)
                    continue
                _append_item(
                    items, field["value"],
                    ["types", type_index, "data", data_index, "data", field_index, "value"],
                    rel, target, source, "database_value",
                    context=field_name,
                    filter_stats=filter_stats,
                )


def _extract_commands(node, path_parts, rel, target, source, items, filter_stats):
    if isinstance(node, dict):
        code = str(node.get("codeStr") or "")
        string_args = node.get("stringArgs")
        if code == "StringCondition" and isinstance(string_args, list):
            for value in string_args:
                if isinstance(value, str) and _is_translation_candidate(value):
                    _record_filter_skip(filter_stats, "string_condition")
            return
        include = code in _NORMAL_COMMANDS or (code == "Picture" and _picture_contains_text(node))
        if include and isinstance(string_args, list):
            for index, value in enumerate(string_args):
                if isinstance(value, str):
                    _append_item(
                        items, value, path_parts + ["stringArgs", index],
                        rel, target, source, "command", context=code, filter_stats=filter_stats,
                    )
            return
        for key, value in node.items():
            _extract_commands(value, path_parts + [key], rel, target, source, items, filter_stats)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _extract_commands(value, path_parts + [index], rel, target, source, items, filter_stats)


def _picture_contains_text(command: dict) -> bool:
    args = command.get("intArgs")
    if not isinstance(args, list) or not args or not isinstance(args[0], int):
        return False
    return ((args[0] >> 4) & 0x07) == 2


def _append_item(
    items: list[TextItem],
    raw: str,
    pointer_parts: list,
    json_rel: str,
    target_file: str,
    source_binary: str,
    role: str,
    context: str = "",
    filter_stats: dict[str, int] | None = None,
) -> None:
    normalized, prefix, suffix, eol = _normalize_text(raw)
    if not normalized.strip():
        return
    skip_reason = classify_wolf_text(normalized)
    if skip_reason:
        if _is_translation_candidate(normalized):
            _record_filter_skip(filter_stats, skip_reason)
        return
    probe = normalized.replace("\n", " ")
    if not is_translatable(probe):
        return
    pointer = _pointer(pointer_parts)
    items.append(TextItem(
        file=target_file,
        key=pointer,
        original=normalized,
        context=f"WOLF {role}" + (f" / {context}" if context else ""),
        meta={
            "kind": _translation_kind(role, context),
            "wolf_json": json_rel,
            "wolf_pointer": pointer,
            "wolf_source_binary": source_binary,
            "wolf_role": role,
            "wolf_prefix": prefix,
            "wolf_suffix": suffix,
            "wolf_eol": eol,
        },
    ))


def _translation_kind(role: str, context: str) -> str:
    if role in {"game", "database_value"}:
        return "ui/system"
    if context == "Choices":
        return "choice"
    if context == "Picture":
        return "ui/system"
    return "message"


def _record_filter_skip(filter_stats: dict[str, int] | None, reason: str) -> None:
    if filter_stats is not None:
        filter_stats[reason] = int(filter_stats.get(reason, 0)) + 1


def _is_translation_candidate(raw: str) -> bool:
    normalized, _, _, _ = _normalize_text(raw)
    return bool(normalized.strip()) and is_translatable(normalized.replace("\n", " "))


def _normalize_text(raw: str) -> tuple[str, str, str, str]:
    leading = _LEADING_BREAKS.match(raw)
    trailing = _TRAILING_BREAKS.search(raw)
    prefix = leading.group(0) if leading else ""
    suffix = trailing.group(0) if trailing and trailing.start() >= len(prefix) else ""
    end = len(raw) - len(suffix) if suffix else len(raw)
    core = raw[len(prefix):end]
    directive = _MESSAGE_DIRECTIVE.match(core)
    if directive:
        prefix += directive.group(0)
        core = core[directive.end():]
    if "\r\r" in raw:
        eol = "\r\r"
    elif "\r\n" in raw:
        eol = "\r\n"
    elif "\r" in raw:
        eol = "\r"
    elif "\n" in raw:
        eol = "\n"
    else:
        eol = "\r\r"
    normalized = core.replace("\r\n", "\n").replace("\r\r", "\n").replace("\r", "\n")
    return normalized, prefix, suffix, eol


def _pointer(parts: list) -> str:
    def escape(value) -> str:
        return str(value).replace("~", "~0").replace("/", "~1")
    return "/" + "/".join(escape(part) for part in parts)


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer: {pointer}")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _get_pointer(data, pointer: str):
    current = data
    for part in _pointer_parts(pointer):
        current = current[int(part)] if isinstance(current, list) else current[part]
    return current


def _set_pointer(data, pointer: str, value) -> None:
    parts = _pointer_parts(pointer)
    current = data
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def _is_database_record_alias(data, pointer: str) -> bool:
    parts = _pointer_parts(pointer)
    if (
        len(parts) != 7
        or parts[0] != "types"
        or parts[2] != "data"
        or parts[4] != "data"
        or parts[6] != "value"
    ):
        return False
    try:
        record = data["types"][int(parts[1])]["data"][int(parts[3])]
        field = record["data"][int(parts[5])]
    except (KeyError, IndexError, TypeError, ValueError):
        return False
    record_name = record.get("name")
    field_value = field.get("value")
    return bool(record_name) and isinstance(field_value, str) and field_value == record_name
