"""Semantic visible-text extraction for RPG Maker RGSS event commands.

MTool exposes a similar boundary through ``MapDataRGSS`` and
``eventCommandListProc``.  This module keeps the boundary narrow: it reads
already-decoded Marshal objects, follows object links, and returns only
parameters known to be displayed by RPG Maker's event interpreter.
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.rgss_scripts import MarshalNode, parse_marshal
from utils.text_extract import is_translatable


@dataclass(frozen=True)
class RGSSVisibleString:
    original: str
    context: str
    event_code: int
    parameter_path: tuple[int, ...]


_EVENT_CLASSES = {"RPG::EventCommand", "RPG::EventCommand"}


def extract_rgss_event_strings(data: bytes) -> list[RGSSVisibleString]:
    """Extract visible event-command text from a Marshal data file.

    The generic Marshal scanner remains the fallback for database fields.  For
    maps/common events this semantic pass prevents command codes, switches,
    resource names and Ruby object metadata from entering the translation
    queue.
    """
    root = parse_marshal(data)
    symbols: list[str] = []
    _collect_symbols(root, symbols)
    links: dict[int, MarshalNode] = {}
    _index_links(root, links)
    result: list[RGSSVisibleString] = []
    _walk_for_event_commands(root, links, symbols, result, set())
    return _dedupe(result)


def _walk_for_event_commands(
    node: MarshalNode,
    links: dict[int, MarshalNode],
    symbols: list[str],
    result: list[RGSSVisibleString],
    seen: set[int],
) -> None:
    if node.type_byte == "@":
        target = links.get(int(node.value))
        if target is not None:
            _walk_for_event_commands(target, links, symbols, result, seen)
        return
    marker = id(node)
    if marker in seen:
        return
    seen.add(marker)

    if node.type_byte == "o":
        klass, ivars = node.children
        class_name = _node_text(klass, links, symbols)
        if class_name == "RPG::EventCommand":
            code = _ivar_value(ivars, "@code", links, symbols)
            params = _ivar_value(ivars, "@parameters", links, symbols)
            if isinstance(code, int) and isinstance(params, list):
                _append_visible(result, code, params)

    for child in _node_children(node):
        _walk_for_event_commands(child, links, symbols, result, seen)


def _append_visible(result: list[RGSSVisibleString], code: int, params: list) -> None:
    # The parameter positions mirror RPG Maker XP/VX/VX Ace's event command
    # interpreter and are the same fields used by the runtime extractor.
    if code == 101 and len(params) > 4 and _visible(params[4]):
        result.append(RGSSVisibleString(str(params[4]), "event.speaker", code, (4,)))
    elif code == 401 and params and _visible(params[0]):
        result.append(RGSSVisibleString(str(params[0]), "event.message", code, (0,)))
    elif code == 102 and params and isinstance(params[0], list):
        for index, value in enumerate(params[0]):
            if _visible(value):
                result.append(RGSSVisibleString(str(value), "event.choice", code, (0, index)))
    elif code == 402 and len(params) > 1 and _visible(params[1]):
        result.append(RGSSVisibleString(str(params[1]), "event.choice", code, (1,)))
    elif code in (105, 405) and params and _visible(params[0]):
        result.append(RGSSVisibleString(str(params[0]), "event.scroll", code, (0,)))
    elif code in (320, 324, 325) and len(params) > 1 and _visible(params[1]):
        role = {320: "event.actor_name", 324: "event.actor_profile", 325: "event.actor_nickname"}[code]
        result.append(RGSSVisibleString(str(params[1]), role, code, (1,)))


def _index_links(node: MarshalNode, links: dict[int, MarshalNode]) -> None:
    """Index object identity positions represented by Marshal links.

    Ruby Marshal object links point to creation order, which is not retained by
    the lossless AST.  Most RPG Maker event data repeats the command object in
    arrays rather than linking it, so the explicit link traversal is kept as a
    best-effort path and never changes extraction correctness for direct nodes.
    """
    if node.object_index is not None:
        links[node.object_index] = node
    for child in _node_children(node):
        _index_links(child, links)


def _collect_symbols(node: MarshalNode, symbols: list[str]) -> None:
    if node.type_byte == ":":
        symbols.append(_decode(node.value))
    for child in _node_children(node):
        _collect_symbols(child, symbols)


def _ivar_value(ivars, name: str, links: dict[int, MarshalNode], symbols: list[str]):
    for key, value in ivars:
        if _node_text(key, links, symbols) == name:
            return _to_value(value, links, symbols)
    return None


def _to_value(node: MarshalNode, links: dict[int, MarshalNode], symbols: list[str]):
    if node.type_byte == "@":
        target = links.get(int(node.value))
        return _to_value(target, links, symbols) if target is not None else None
    if node.type_byte == "i":
        return node.value
    if node.type_byte in ("0", "T", "F"):
        return {"0": None, "T": True, "F": False}[node.type_byte]
    if node.type_byte in ('"', ':'):
        return _decode(node.value)
    if node.type_byte == ";":
        index = int(node.value)
        return symbols[index] if 0 <= index < len(symbols) else None
    if node.type_byte == "I" and isinstance(node.children, tuple):
        return _to_value(node.children[0], links, symbols)
    if node.type_byte == "[" and isinstance(node.children, list):
        return [_to_value(item, links, symbols) for item in node.children]
    if node.type_byte == "{":
        return None
    return None


def _node_text(node: MarshalNode | None, links: dict[int, MarshalNode], symbols: list[str]) -> str:
    value = _to_value(node, links, symbols) if node is not None else None
    return str(value) if isinstance(value, str) else ""


def _visible(value) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 2 and is_translatable(value)


def _decode(raw) -> str:
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    for encoding in ("utf-8", "cp932"):
        try:
            return bytes(raw).decode(encoding)
        except UnicodeDecodeError:
            continue
    return bytes(raw).decode("latin-1")


def _node_children(node: MarshalNode):
    children = node.children
    if isinstance(children, list):
        for child in children:
            if isinstance(child, MarshalNode):
                yield child
            elif isinstance(child, tuple):
                yield from (item for item in child if isinstance(item, MarshalNode))
    elif isinstance(children, tuple):
        for child in children:
            if isinstance(child, MarshalNode):
                yield child
            elif isinstance(child, (list, tuple)):
                for item in child:
                    if isinstance(item, MarshalNode):
                        yield item
                    elif isinstance(item, tuple):
                        yield from (value for value in item if isinstance(value, MarshalNode))


def _dedupe(items: list[RGSSVisibleString]) -> list[RGSSVisibleString]:
    seen: set[tuple[str, int, tuple[int, ...]]] = set()
    result: list[RGSSVisibleString] = []
    for item in items:
        key = (item.original, item.event_code, item.parameter_path)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
