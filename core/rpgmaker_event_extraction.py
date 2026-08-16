"""Visible RPG Maker event-command extraction helpers."""

from __future__ import annotations

import json


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


__all__ = ["_make_ctx", "_scan_event_list", "_texts_with_adjacency"]
