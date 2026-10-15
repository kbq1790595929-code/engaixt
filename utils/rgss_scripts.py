"""Structured RPG Maker RGSS script extraction and rewrite helpers.

RGSS ``Data/Scripts.*`` is a Ruby Marshal array whose third field is a
zlib-compressed Ruby source string.  It must not be treated as an ordinary
Marshal string: doing so exposes compressed bytes as if they were Japanese
text and can corrupt the script table on write.
"""
from __future__ import annotations

import hashlib
import re
import zlib
from dataclasses import dataclass

from utils.rvdata2 import MarshalStream, _verify_version


@dataclass
class MarshalNode:
    type_byte: str
    start: int
    end: int
    value: object = None
    children: object = None
    replacement: bytes | None = None
    object_index: int | None = None

    def changed(self) -> bool:
        if self.replacement is not None:
            return True
        if self.type_byte in ("I", "[", "{", "o", "C", "U", "e"):
            return _children_changed(self.children)
        return False


@dataclass(frozen=True)
class RGSSScript:
    index: int
    script_id: int | None
    name: str
    source: bytes
    source_text: str
    source_encoding: str
    source_node: MarshalNode


@dataclass(frozen=True)
class RubyStringLiteral:
    start: int
    end: int
    original: str
    quote: str
    has_interpolation: bool = False
    content_only: bool = False


_JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PATH_RE = re.compile(r"(?:[\\/]|\.[A-Za-z0-9]{1,6}$)")


def is_ruby_translatable(text: str) -> bool:
    return bool(text and len(text.strip()) >= 2 and _JP_RE.search(text))


def parse_marshal(data: bytes) -> MarshalNode:
    stream = MarshalStream(data)
    _verify_version(stream)
    root = _read_node(stream, [])
    if stream.pos != len(data):
        raise ValueError("Unexpected trailing Ruby Marshal data")
    return root


def extract_rgss_script_literals(data: bytes) -> list[dict]:
    """Return visible Ruby literals from a valid Scripts Marshal object."""
    root = parse_marshal(data)
    result: list[dict] = []
    for script in _iter_scripts(root, data):
        for ordinal, literal in enumerate(extract_ruby_string_literals(script.source_text)):
            result.append({
                "script_index": script.index,
                "script_id": script.script_id,
                "script_name": script.name,
                "source": literal.original,
                "ordinal": ordinal,
                "span_start": literal.start,
                "span_end": literal.end,
                "quote": literal.quote,
                "content_only": literal.content_only,
                "source_encoding": script.source_encoding,
                "script_source_sha256": hashlib.sha256(script.source).hexdigest(),
                "marshal_source_sha256": hashlib.sha256(data).hexdigest(),
            })
    return result


def replace_rgss_script_literals(data: bytes, replacements: list[dict]) -> bytes:
    """Rewrite selected Ruby literals and recompress their script sources.

    Replacement spans are generated during extraction and are checked against
    the current source before any Marshal bytes are changed.  This avoids the
    ambiguous global ``original -> translated`` mapping when two scripts use
    the same Japanese string.
    """
    root = parse_marshal(data)
    grouped: dict[int, list[dict]] = {}
    for item in replacements:
        translated = str(item.get("translated") or "")
        original = str(item.get("original") or "")
        if translated and translated != original:
            grouped.setdefault(int(item["script_index"]), []).append(item)

    scripts = {script.index: script for script in _iter_scripts(root, data)}
    for index, updates in grouped.items():
        script = scripts.get(index)
        if script is None:
            raise ValueError(f"RGSS script index no longer exists: {index}")
        expected_hashes = {str(item.get("source_sha256") or "") for item in updates} - {""}
        actual_hash = hashlib.sha256(script.source).hexdigest()
        if expected_hashes and expected_hashes != {actual_hash}:
            raise ValueError(f"RGSS script source changed: {script.name or index}")

        literals = extract_ruby_string_literals(script.source_text)
        ordered = sorted(updates, key=lambda item: int(item["span_start"]), reverse=True)
        source_text = script.source_text
        for item in ordered:
            start = int(item["span_start"])
            end = int(item["span_end"])
            if start < 0 or end > len(source_text) or start >= end:
                raise ValueError(f"Invalid RGSS script literal span: {start}:{end}")
            match = next((literal for literal in literals
                          if literal.start == start and literal.end == end), None)
            if match is None or match.original != str(item.get("original") or ""):
                raise ValueError(f"RGSS script literal no longer matches: {item.get('original')!r}")
            replacement = _encode_ruby_literal(str(item["translated"]), match.quote)
            if match.content_only:
                replacement = replacement[1:-1]
            source_text = source_text[:start] + replacement + source_text[end:]
        source_bytes, _encoding = _encode_source(source_text, script.source_encoding)
        compressed = zlib.compress(source_bytes)
        script.source_node.replacement = compressed

    return data[:2] + _serialize_marshal(data, root)


def extract_ruby_string_literals(source: str) -> list[RubyStringLiteral]:
    """Lex ordinary Ruby strings without treating comments or code as text.

    Interpolated expressions stay unchanged; simple interpolation exposes its
    surrounding text as separate content spans. Source paths and reflection
    calls are excluded.
    """
    result: list[RubyStringLiteral] = []
    i = 0
    n = len(source)
    while i < n:
        char = source[i]
        if char == "#":
            newline = source.find("\n", i + 1)
            i = n if newline < 0 else newline + 1
            continue
        if char in ("'", '"'):
            start = i
            end, raw, interpolation = _scan_quoted(source, i, char)
            if end is None:
                break
            i = end
            if _looks_like_code_string(source, start, end):
                continue
            if interpolation:
                result.extend(_interpolation_fragments(source, start, end))
                continue
            text = _decode_ruby_string(raw, char)
            if _is_literal_text(text):
                result.append(RubyStringLiteral(start, end, text, char, False))
            continue
        if char == "%" and i + 2 < n and source[i + 1] in "qQ" and not _escaped(source, i):
            start = i
            end, raw, interpolation, quote = _scan_percent_string(source, i)
            if end is None:
                break
            i = end
            if interpolation or _looks_like_code_string(source, start, end):
                continue
            text = _decode_ruby_string(raw, quote)
            if _is_literal_text(text):
                result.append(RubyStringLiteral(start, end, text, quote, False))
            continue
        i += 1
    return result


def _iter_scripts(root: MarshalNode, data: bytes):
    if root.type_byte != "[" or not isinstance(root.children, list):
        return
    for index, entry in enumerate(root.children):
        if entry.type_byte != "[" or not isinstance(entry.children, list) or len(entry.children) < 3:
            continue
        fields = entry.children
        script_id = _node_integer(fields[0])
        name_node = _string_node(fields[1])
        source_node = _string_node(fields[2])
        if source_node is None:
            continue
        compressed = _node_bytes(source_node, data)
        try:
            source = zlib.decompress(compressed)
        except zlib.error:
            continue
        source_text, encoding = _decode_source(source)
        name = _decode_bytes(_node_bytes(name_node, data)) if name_node else ""
        yield RGSSScript(index, script_id, name, source, source_text, encoding, source_node)


def _string_node(node: MarshalNode | None) -> MarshalNode | None:
    if node is None:
        return None
    if node.type_byte == '"':
        return node
    if node.type_byte == "I" and isinstance(node.children, tuple):
        return _string_node(node.children[0])
    return None


def _node_bytes(node: MarshalNode | None, data: bytes) -> bytes:
    if node is None or node.type_byte != '"':
        return b""
    return bytes(node.value or b"")


def _node_integer(node: MarshalNode) -> int | None:
    return int(node.value) if node.type_byte == "i" and isinstance(node.value, int) else None


def _decode_source(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return raw.decode("cp932"), "cp932"
        except UnicodeDecodeError:
            return raw.decode("latin-1"), "latin-1"


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _encode_source(source: str, preferred: str) -> tuple[bytes, str]:
    for encoding in (preferred, "utf-8"):
        try:
            return source.encode(encoding), encoding
        except UnicodeEncodeError:
            continue
    return source.encode("utf-8", errors="replace"), "utf-8"


def _is_literal_text(text: str) -> bool:
    if not is_ruby_translatable(text):
        return False
    return not _PATH_RE.search(text.strip())


def _looks_like_code_string(source: str, start: int, end: int) -> bool:
    prefix = source[max(0, start - 100):start]
    stripped = prefix.rstrip()
    return bool(re.search(
        r"(?:require|load|autoload|open|File\.[A-Za-z_]+|Dir\.[A-Za-z_]+|"
        r"eval|send|public_send|respond_to\?|instance_variable_get|"
        r"instance_variable_set|const_get|method)\s*\(?\s*$",
        stripped,
    ))


def _scan_quoted(source: str, start: int, quote: str):
    i = start + 1
    raw_start = i
    interpolation = False
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if quote == '"' and source.startswith("#{", i):
            interpolation = True
            expression_end = _interpolation_end(source, i + 2)
            if expression_end is None:
                return None, "", True
            i = expression_end
            continue
        if source[i] == quote:
            return i + 1, source[raw_start:i], interpolation
        i += 1
    return None, "", interpolation


def _interpolation_end(source: str, start: int) -> int | None:
    depth = 1
    index = start
    while index < len(source):
        char = source[index]
        if char in ("'", '"'):
            end, _, _ = _scan_quoted(source, index, char)
            if end is None:
                return None
            index = end
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _interpolation_fragments(source: str, start: int, end: int) -> list[RubyStringLiteral]:
    fragments: list[RubyStringLiteral] = []
    cursor = start + 1
    index = cursor
    while index < end - 1:
        if source[index] == "\\":
            index += 2
            continue
        if source.startswith("#{", index):
            expression_end = _interpolation_end(source, index + 2)
            if expression_end is None or expression_end >= end:
                return []
            if _is_literal_text(text := _decode_ruby_string(source[cursor:index], '"')):
                fragments.append(RubyStringLiteral(cursor, index, text, '"', True, True))
            cursor = expression_end
            index = cursor
            continue
        index += 1
    if _is_literal_text(text := _decode_ruby_string(source[cursor:end - 1], '"')):
        fragments.append(RubyStringLiteral(cursor, end - 1, text, '"', True, True))
    return fragments


def _scan_percent_string(source: str, start: int):
    kind = source[start + 1]
    opening = source[start + 2]
    closing = {"(": ")", "[": "]", "{": "}", "<": ">"}.get(opening, opening)
    i = start + 3
    raw_start = i
    depth = 1 if opening != closing else 0
    interpolation = False
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if kind == "Q" and source.startswith("#{", i):
            interpolation = True
        if opening != closing and source[i] == opening:
            depth += 1
        elif source[i] == closing:
            if depth:
                depth -= 1
                if depth:
                    i += 1
                    continue
            return i + 1, source[raw_start:i], interpolation, '"'
        i += 1
    return None, "", interpolation, '"'


def _decode_ruby_string(raw: str, quote: str) -> str:
    output: list[str] = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "f": "\f", "v": "\v", "a": "\a", "e": "\x1b"}
    while i < len(raw):
        if raw[i] != "\\" or i + 1 >= len(raw):
            output.append(raw[i])
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in escapes:
            output.append(escapes[nxt])
        elif nxt in ("\\", "'", '"'):
            output.append(nxt)
        elif nxt == "x" and i + 3 < len(raw):
            try:
                output.append(chr(int(raw[i + 2:i + 4], 16)))
                i += 2
            except ValueError:
                output.append(nxt)
        else:
            output.append(nxt)
        i += 2
    return "".join(output)


def _encode_ruby_literal(text: str, quote: str) -> str:
    escaped = text.replace("\\", "\\\\")
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if quote == "'":
        return "'" + escaped.replace("'", "\\'") + "'"
    escaped = escaped.replace('"', '\\"').replace("#{", "\\#{")
    return '"' + escaped + '"'


def _escaped(source: str, index: int) -> bool:
    count = 0
    index -= 1
    while index >= 0 and source[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _read_node(stream: MarshalStream, objects: list[MarshalNode]) -> MarshalNode:
    def register(node: MarshalNode) -> MarshalNode:
        node.object_index = len(objects)
        objects.append(node)
        return node

    start = stream.pos
    type_byte = stream.read_type()
    if type_byte in ("0", "T", "F"):
        return MarshalNode(type_byte, start, stream.pos, value=None)
    if type_byte in (";", "@"):
        value = stream.read_long()
        return MarshalNode(type_byte, start, stream.pos, value=value)
    if type_byte == "i":
        from utils.rvdata2 import _read_fixnum
        value = _read_fixnum(stream)
        return MarshalNode(type_byte, start, stream.pos, value=value)
    if type_byte == "l":
        sign = stream.read_byte()
        count = stream.read_long()
        raw = stream.read_bytes(count * 2)
        return MarshalNode(type_byte, start, stream.pos, value=(sign, count, raw))
    if type_byte == '"':
        length = stream.read_long()
        raw = stream.read_bytes(length)
        return register(MarshalNode(type_byte, start, stream.pos, value=raw))
    if type_byte == ":":
        length = stream.read_long()
        raw = stream.read_bytes(length)
        return MarshalNode(type_byte, start, stream.pos, value=raw)
    if type_byte == "f":
        length = stream.read_long()
        raw = stream.read_bytes(length)
        return MarshalNode(type_byte, start, stream.pos, value=raw)
    if type_byte == "I":
        inner = _read_node(stream, objects)
        count = stream.read_long()
        ivars = [(_read_node(stream, objects), _read_node(stream, objects)) for _ in range(count)]
        return MarshalNode(type_byte, start, stream.pos, children=(inner, ivars), value=count)
    if type_byte == "[":
        count = stream.read_long()
        node = register(MarshalNode(type_byte, start, start, children=[], value=count))
        node.children = [_read_node(stream, objects) for _ in range(count)]
        node.end = stream.pos
        return node
    if type_byte == "{":
        count = stream.read_long()
        node = register(MarshalNode(type_byte, start, start, children=[], value=count))
        node.children = [(_read_node(stream, objects), _read_node(stream, objects)) for _ in range(count)]
        node.end = stream.pos
        return node
    if type_byte == "o":
        node = register(MarshalNode(type_byte, start, start, children=None))
        klass = _read_node(stream, objects)
        count = stream.read_long()
        ivars = [(_read_node(stream, objects), _read_node(stream, objects)) for _ in range(count)]
        node.children = (klass, ivars)
        node.value = count
        node.end = stream.pos
        return node
    if type_byte in ("C", "U", "e"):
        node = register(MarshalNode(type_byte, start, start, children=None))
        node.children = (_read_node(stream, objects), _read_node(stream, objects))
        node.end = stream.pos
        return node
    if type_byte == "u":
        node = register(MarshalNode(type_byte, start, start, children=None))
        klass = _read_node(stream, objects)
        length = stream.read_long()
        raw = stream.read_bytes(length)
        node.children = klass
        node.value = raw
        node.end = stream.pos
        return node
    raise ValueError(f"Unsupported Ruby Marshal type: {type_byte!r}")


def _children_changed(children) -> bool:
    if isinstance(children, list):
        return any(_children_changed(child) for child in children)
    if isinstance(children, tuple):
        return any(_children_changed(child) for child in children)
    if isinstance(children, MarshalNode):
        return children.changed()
    return False


def _serialize_marshal(data: bytes, node: MarshalNode) -> bytes:
    if not node.changed():
        return data[node.start:node.end]
    return _serialize_node(data, node)


def _serialize_node(data: bytes, node: MarshalNode) -> bytes:
    t = node.type_byte
    if t == '"' and node.replacement is not None:
        return b'"' + _write_long(len(node.replacement)) + node.replacement
    if t == "I":
        inner, ivars = node.children
        out = bytearray(b"I")
        out.extend(_serialize_marshal(data, inner))
        out.extend(_write_long(len(ivars)))
        for key, value in ivars:
            out.extend(_serialize_marshal(data, key))
            out.extend(_serialize_marshal(data, value))
        return bytes(out)
    if t == "[":
        out = bytearray(b"[")
        out.extend(_write_long(len(node.children)))
        for child in node.children:
            out.extend(_serialize_marshal(data, child))
        return bytes(out)
    if t == "{":
        out = bytearray(b"{")
        out.extend(_write_long(len(node.children)))
        for key, value in node.children:
            out.extend(_serialize_marshal(data, key))
            out.extend(_serialize_marshal(data, value))
        return bytes(out)
    if t == "o":
        klass, ivars = node.children
        out = bytearray(b"o")
        out.extend(_serialize_marshal(data, klass))
        out.extend(_write_long(len(ivars)))
        for key, value in ivars:
            out.extend(_serialize_marshal(data, key))
            out.extend(_serialize_marshal(data, value))
        return bytes(out)
    if t in ("C", "U", "e"):
        left, right = node.children
        return t.encode("ascii") + _serialize_marshal(data, left) + _serialize_marshal(data, right)
    if t == "u":
        klass = node.children
        raw = node.value
        return b"u" + _serialize_marshal(data, klass) + _write_long(len(raw)) + raw
    return data[node.start:node.end]


def _write_long(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    if 0 < value < 123:
        return bytes([value + 5])
    if -124 < value < 0:
        return bytes([(value - 5) & 0xff])
    for size in range(1, 5):
        if (0 <= value < (1 << (size * 8))) or (-(1 << (size * 8)) <= value < 0):
            return bytes([size if value >= 0 else 256 - size]) + (value & ((1 << (size * 8)) - 1)).to_bytes(size, "little")
    raise ValueError("Ruby Marshal integer exceeds 32-bit long range")
