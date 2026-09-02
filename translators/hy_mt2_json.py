"""Strict-but-recoverable JSON handling for the local Hy-MT2 backend.

The cloud translators have their own response parser and recovery policy. This
module is intentionally local-only: llama.cpp responses may include a harmless
preface, a Markdown fence, or end mid-object when the generation budget is
reached. Recovery is keyed by the explicit ``N:text_type`` IDs emitted in the
prompt, so response order can never move a translation onto another item.
"""
from __future__ import annotations

import json
import re

from translators.deepseek_batch_json import BatchJsonParseError, BatchShapeError


_KEY_ID_RE = re.compile(r"^(\d+)(?::[^:]+)?$")


def _reject_duplicate_keys(pairs: list[tuple[object, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise BatchShapeError(f"duplicate response key: {key}")
        result[key] = value
    return result


def _extract_object(text: str) -> str | None:
    """Find the first complete JSON object without guessing missing bytes."""
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_keys)
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            _value, end = decoder.raw_decode(text, start)
        except (json.JSONDecodeError, BatchShapeError):
            continue
        return text[start:end]
    return None


def _normalize_response_key(key: object, expected_keys: list[str]) -> str | None:
    if not isinstance(key, str):
        return None
    if key in expected_keys:
        return key
    # Older local checkpoints/model responses sometimes dropped the type suffix
    # ("1:message" -> "1"). The numeric ID is still explicit and unambiguous.
    match = _KEY_ID_RE.fullmatch(key.strip())
    if not match:
        return None
    row_id = int(match.group(1))
    matches = [expected for expected in expected_keys if expected.split(":", 1)[0] == str(row_id)]
    return matches[0] if len(matches) == 1 else None


def _key_to_id(key: str) -> int:
    match = _KEY_ID_RE.fullmatch(key)
    if not match:
        raise BatchShapeError(f"invalid response key: {key}")
    return int(match.group(1))


def _load_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        raise BatchJsonParseError("empty local response")
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except BatchShapeError:
        raise
    except json.JSONDecodeError as exc:
        extracted = _extract_object(text)
        if not extracted:
            raise BatchJsonParseError(f"invalid local JSON: {exc}") from exc
        try:
            data = json.loads(extracted, object_pairs_hook=_reject_duplicate_keys)
        except (json.JSONDecodeError, BatchShapeError) as extracted_error:
            if isinstance(extracted_error, BatchShapeError):
                raise extracted_error
            raise BatchJsonParseError(f"invalid extracted local JSON: {extracted_error}") from extracted_error
    if not isinstance(data, dict):
        raise BatchShapeError("local response must be a JSON object")
    return data


def parse_local_translation_map(raw: str, expected_keys: list[str]) -> dict[int, str]:
    """Parse a complete local response, independent of object key order."""
    data = _load_object(raw)
    normalized: dict[str, str] = {}
    for key, value in data.items():
        normalized_key = _normalize_response_key(key, expected_keys)
        if normalized_key is None:
            raise BatchShapeError(f"unknown or ambiguous response key: {key}")
        if normalized_key in normalized:
            raise BatchShapeError(f"duplicate normalized response key: {normalized_key}")
        if not isinstance(value, str):
            raise BatchShapeError(f"translation for {key} is not a string")
        normalized[normalized_key] = value.strip()
    if set(normalized) != set(expected_keys):
        raise BatchShapeError("local response ID set mismatch")
    return {_key_to_id(key): normalized[key] for key in expected_keys}


def parse_partial_local_translation_map(raw: str, expected_keys: list[str]) -> dict[int, str]:
    """Recover only complete key/value pairs from a truncated local object.

    The parser stops at the first incomplete pair and never assigns values by
    position. A recovered value is still subject to the normal translation and
    control-token validator before it is stored.
    """
    text = str(raw or "")
    start = text.find("{")
    if start < 0:
        return {}
    decoder = json.JSONDecoder()
    expected = set(expected_keys)
    recovered: dict[str, str] = {}
    position = start + 1
    while position < len(text):
        while position < len(text) and (text[position].isspace() or text[position] == ","):
            position += 1
        if position >= len(text) or text[position] == "}":
            break
        try:
            key, next_position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        position = next_position
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text) or text[position] != ":":
            break
        position += 1
        while position < len(text) and text[position].isspace():
            position += 1
        try:
            value, next_position = decoder.raw_decode(text, position)
        except json.JSONDecodeError:
            break
        position = next_position
        normalized_key = _normalize_response_key(key, expected_keys)
        if normalized_key in expected and isinstance(value, str):
            if normalized_key in recovered:
                recovered.pop(normalized_key, None)
            else:
                recovered[normalized_key] = value.strip()
    return {_key_to_id(key): value for key, value in recovered.items()}


__all__ = [
    "parse_local_translation_map",
    "parse_partial_local_translation_map",
]
