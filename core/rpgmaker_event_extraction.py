"""Visible RPG Maker event-command extraction helpers."""

from __future__ import annotations

import json
import re


RPGMAKER_MESSAGE_CONTRACT = "rpgmaker_message_v2"
RPGMAKER_MESSAGE_SEPARATOR = "__RPGM_SEGMENT__"

_LEADING_SPEAKER_RE = re.compile(
    r"^(?P<prefix>(?:\\n|\n)?)<(?P<name>[^<>\r\n]{1,256})>"
)


_CHOICE_HELP_MARKERS = {
    "choicehelp",
    "<choicehelp>",
    "選択肢ヘルプ",
    "<選択肢ヘルプ>",
}
_PLUGIN_VISIBLE_TEXT_KEYS = {
    "text",
    "messagetext",
    "helptext",
    "label",
    "description",
}
_PLUGIN_VISIBLE_CONTAINER_KEYS = {"choices", "options", "items"}


def _make_ctx(cat: str, id_val=None, name: str = "", role: str = "") -> str:
    parts = [cat]
    if id_val is not None and id_val != "":
        parts.append(str(id_val))
    if name:
        parts.append(name)
    if role:
        parts.append(role)
    return ".".join(parts)


def _is_choice_help_marker(value: object) -> bool:
    return str(value or "").strip().lower() in _CHOICE_HELP_MARKERS


def _plugin_visible_texts(args: object) -> list[str]:
    """Extract only explicitly visible strings from MZ plugin command args."""
    result: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            result.append(value)

    def walk_container(
        value: object,
        *,
        plain_strings_visible: bool = False,
        depth: int = 0,
    ) -> None:
        if depth > 8:
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in {"[", "{"}:
                try:
                    decoded = json.loads(stripped)
                except json.JSONDecodeError:
                    decoded = None
                if decoded is not None:
                    walk_container(
                        decoded,
                        plain_strings_visible=plain_strings_visible,
                        depth=depth + 1,
                    )
                    return
            if plain_strings_visible:
                add(value)
            return
        if isinstance(value, list):
            for item in value:
                walk_container(item, plain_strings_visible=True, depth=depth + 1)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            normalized = str(key or "").replace("_", "").lower()
            if normalized in _PLUGIN_VISIBLE_TEXT_KEYS:
                add(item)
            elif normalized in _PLUGIN_VISIBLE_CONTAINER_KEYS:
                walk_container(item, plain_strings_visible=True, depth=depth + 1)

    if isinstance(args, dict):
        for key, value in args.items():
            normalized = str(key or "").replace("_", "").lower()
            if normalized in _PLUGIN_VISIBLE_TEXT_KEYS:
                add(value)
            elif normalized in _PLUGIN_VISIBLE_CONTAINER_KEYS:
                walk_container(value, plain_strings_visible=True)
    return result


def _scan_event_list(event_list: list, context: str) -> list[tuple[str, str, int]]:
    """Extract visible strings as ``(text, context, command_index)`` tuples."""
    texts: list[tuple[str, str, int]] = []
    if not event_list:
        return texts

    for index, command in enumerate(event_list):
        if not command or "code" not in command:
            continue
        code = command["code"]
        params = command.get("parameters", [])

        if code == 101 and len(params) > 4 and params[4]:
            texts.append((str(params[4]), context + ".speaker", index))
        elif code == 401 and params and params[0]:
            texts.append((str(params[0]), context + ".line", index))
        elif code == 102 and params and params[0]:
            for choice in params[0]:
                if choice:
                    texts.append((str(choice), context + ".choice", index))
        elif code == 402 and len(params) > 1 and params[1]:
            texts.append((str(params[1]), context + ".choice", index))
        elif code == 105 and params and params[0]:
            texts.append((str(params[0]), context + ".scroll", index))
        elif code == 405 and params and params[0]:
            texts.append((str(params[0]), context + ".scroll", index))
        elif code == 108 and params and _is_choice_help_marker(params[0]):
            for follow in event_list[index + 1:]:
                if not follow or follow.get("code") != 408:
                    break
                follow_params = follow.get("parameters", [])
                if follow_params and follow_params[0]:
                    texts.append((str(follow_params[0]), context + ".choice_help", index))
        elif code == 320 and len(params) > 1 and params[1]:
            texts.append((str(params[1]), context + ".name", index))
        elif code in {324, 325} and len(params) > 1 and params[1]:
            texts.append((str(params[1]), context + ".profile", index))
        elif code == 357 and len(params) > 3:
            for visible_text in _plugin_visible_texts(params[3]):
                texts.append((visible_text, context + ".plugin_text", index))

    return texts


def _scan_event_records(event_list: list, context: str) -> list[dict]:
    """Extract event text while keeping each message box as one unit.

    RPG Maker stores one visible message as a ``101`` command followed by one
    or more ``401`` screen lines. Translating those lines independently lets a
    model merge a sentence and shift every following result by one id. The
    grouped record keeps the authored screen-line boundaries in metadata so the
    runtime map can split the translated message back into the original calls.
    """
    records: list[dict] = []
    if not event_list:
        return records

    def add(text: object, role: str, index: int, meta: dict | None = None) -> None:
        if text:
            records.append({
                "text": str(text),
                "context": context + role,
                "command_index": index,
                "meta": dict(meta or {}),
            })

    index = 0
    while index < len(event_list):
        command = event_list[index]
        if not command or "code" not in command:
            index += 1
            continue
        code = command["code"]
        params = command.get("parameters", [])

        if code in {101, 401}:
            start_index = index
            if code == 101 and len(params) > 4 and params[4]:
                add(
                    params[4],
                    ".speaker",
                    index,
                    {
                        "rpgmaker_speaker_name": True,
                        "translation_cache_scope": "rpgmaker_speaker_v1",
                        "translation_contract": "rpgmaker_speaker_v1",
                    },
                )
            cursor = index + 1 if code == 101 else index
            segments: list[str] = []
            while cursor < len(event_list):
                follow = event_list[cursor]
                if not follow or follow.get("code") != 401:
                    break
                follow_params = follow.get("parameters", [])
                if follow_params and follow_params[0]:
                    segments.append(str(follow_params[0]))
                cursor += 1
            if segments:
                speaker = ""
                speaker_match = _LEADING_SPEAKER_RE.match(segments[0])
                if speaker_match:
                    speaker = speaker_match.group("name")
                    add(
                        speaker,
                        ".name",
                        start_index,
                        {
                            "rpgmaker_speaker_name": True,
                            "translation_cache_scope": "rpgmaker_speaker_v1",
                            "translation_contract": "rpgmaker_speaker_v1",
                        },
                    )
                add(
                    RPGMAKER_MESSAGE_SEPARATOR.join(segments),
                    ".line",
                    start_index,
                    {
                        "rpgmaker_segments": segments,
                        "rpgmaker_speaker": speaker,
                        "translation_cache_scope": RPGMAKER_MESSAGE_CONTRACT,
                        "translation_contract": RPGMAKER_MESSAGE_CONTRACT,
                    },
                )
                index = cursor
                continue
        elif code == 102 and params and params[0]:
            for choice in params[0]:
                add(choice, ".choice", index)
        elif code == 402 and len(params) > 1 and params[1]:
            add(params[1], ".choice", index)
        elif code == 105 and params and params[0]:
            add(params[0], ".scroll", index)
        elif code == 405 and params and params[0]:
            add(params[0], ".scroll", index)
        elif code == 108 and params and _is_choice_help_marker(params[0]):
            for follow in event_list[index + 1:]:
                if not follow or follow.get("code") != 408:
                    break
                follow_params = follow.get("parameters", [])
                if follow_params and follow_params[0]:
                    add(follow_params[0], ".choice_help", index)
        elif code == 320 and len(params) > 1 and params[1]:
            add(params[1], ".name", index)
        elif code in {324, 325} and len(params) > 1 and params[1]:
            add(params[1], ".profile", index)
        elif code == 357 and len(params) > 3:
            for visible_text in _plugin_visible_texts(params[3]):
                add(visible_text, ".plugin_text", index)

        index += 1

    return records


def _records_with_adjacency(records: list[dict]) -> list[dict]:
    result: list[dict] = []
    for index, record in enumerate(records):
        item = dict(record)
        item["prev_text"] = records[index - 1]["text"] if index > 0 else ""
        item["next_text"] = records[index + 1]["text"] if index < len(records) - 1 else ""
        result.append(item)
    return result


def _texts_with_adjacency(texts: list[tuple[str, str, int]]) -> list[dict]:
    result = []
    for index, (text, context, _) in enumerate(texts):
        previous = texts[index - 1][0] if index > 0 else ""
        following = texts[index + 1][0] if index < len(texts) - 1 else ""
        result.append({
            "text": text,
            "context": context,
            "prev_text": previous,
            "next_text": following,
        })
    return result


__all__ = [
    "RPGMAKER_MESSAGE_CONTRACT",
    "RPGMAKER_MESSAGE_SEPARATOR",
    "_make_ctx",
    "_records_with_adjacency",
    "_scan_event_list",
    "_scan_event_records",
    "_texts_with_adjacency",
]
