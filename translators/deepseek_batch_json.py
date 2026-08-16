from __future__ import annotations

import json
import re
from typing import Any


class BatchJsonParseError(ValueError):
    pass


class BatchShapeError(ValueError):
    pass


def parse_batch_json(raw: str, expected_ids: list[int]) -> dict[int, str]:
    text = raw.strip()
    if text.startswith("```"):
        raise BatchJsonParseError("response contains markdown fence")
    if not (
        (text.startswith("{") and text.endswith("}"))
        or (text.startswith("[") and text.endswith("]"))
    ):
        extracted = _extract_json_payload(text)
        if not extracted:
            raise BatchJsonParseError("response does not contain json payload")
        text = extracted
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatchJsonParseError(f"invalid json: {exc}") from exc
    if isinstance(data, dict):
        if set(data.keys()) == {"t"} and isinstance(data.get("t"), dict):
            return _parse_translation_map(data["t"], expected_ids)
        if set(data.keys()) != {"items"}:
            raise BatchShapeError("response object must only contain t or items")
        data = data.get("items")
    if not isinstance(data, list):
        raise BatchJsonParseError("response is not array")

    result: dict[int, str] = {}
    for row in data:
        if not isinstance(row, dict) or set(row.keys()) - {"id", "t"}:
            raise BatchShapeError("invalid row shape")
        if "id" not in row or "t" not in row:
            raise BatchShapeError("missing id or t")
        try:
            row_id = int(row["id"])
        except Exception as exc:
            raise BatchShapeError("id is not int") from exc
        if row_id in result:
            raise BatchShapeError("duplicate id")
        if not isinstance(row["t"], str):
            raise BatchShapeError("translation is not string")
        result[row_id] = row["t"].strip()
    _validate_id_set(result, expected_ids)
    return result


def parse_partial_translation_map(raw: str, expected_ids: list[int]) -> dict[int, str]:
    """Recover complete id/value pairs from a truncated {"t": {...}} response.

    This deliberately uses JSONDecoder for both keys and values. It stops at
    the first incomplete pair and never guesses or repairs translation text.
    """
    match = re.search(r'"t"\s*:\s*\{', str(raw or ""))
    if not match:
        return {}
    decoder = json.JSONDecoder()
    expected = set(expected_ids)
    result: dict[int, str] = {}
    position = match.end()
    text = str(raw)
    while position < len(text):
        while position < len(text) and (text[position].isspace() or text[position] == ","):
            position += 1
        if position >= len(text) or text[position] == "}":
            break
        try:
            key, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != ":":
            break
        position += 1
        while position < len(text) and text[position].isspace():
            position += 1
        try:
            value, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        try:
            row_id = int(key)
        except (TypeError, ValueError):
            continue
        if row_id in expected and isinstance(value, str) and row_id not in result:
            result[row_id] = value.strip()
    return result


def _parse_translation_map(data: dict[Any, Any], expected_ids: list[int]) -> dict[int, str]:
    result: dict[int, str] = {}
    for key, value in data.items():
        try:
            row_id = int(key)
        except Exception as exc:
            raise BatchShapeError("id key is not int") from exc
        if row_id in result:
            raise BatchShapeError("duplicate id")
        if not isinstance(value, str):
            raise BatchShapeError("translation is not string")
        result[row_id] = value.strip()
    _validate_id_set(result, expected_ids)
    return result


def _validate_id_set(result: dict[int, str], expected_ids: list[int]) -> None:
    if set(result) != set(expected_ids):
        raise BatchShapeError("id set mismatch")


def _extract_json_payload(text: str) -> str | None:
    starts = [
        (position, opener, closer)
        for position, opener, closer in (
            (text.find("{"), "{", "}"),
            (text.find("["), "[", "]"),
        )
        if position >= 0
    ]
    for start, opener, closer in sorted(starts, key=lambda row: row[0]):
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            char = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return text[start:position + 1].strip()
    return None
