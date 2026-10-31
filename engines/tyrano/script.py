"""Structure-aware extraction and patching for TyranoScript ``.ks`` files."""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.text_extract import is_translatable


_KANA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]")
TYRANO_SPAN_CONTRACT = "tyrano_visible_spans_v5"
_TAG_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*")
_AT_TAG_RE = re.compile(r"^\s*@\s*([A-Za-z_][A-Za-z0-9_.:-]*)\b")
_END_TAG_AT_LINE_END_RE = re.compile(r"\[\s*(?:endscript|endhtml)(?:\s+[^\]]*)?\]\s*$", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*"
    r"(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|"
    r"'(?P<single>(?:\\.|[^'\\])*)'|"
    r"`(?P<backtick>(?:\\.|[^`\\])*)`|"
    r"(?P<unquoted>[^\s\[\]\"'`]+))"
)
_VARIABLE_ASSIGNMENT_RE = re.compile(
    r"(?<![\w$])f\s*(?:\.\s*(?P<dot_key>[\w$]+)|"
    r"\[\s*(['\"])(?P<bracket_key>[\w$]+)\2\s*\])\s*=\s*"
)
_VARIABLE_REFERENCE_RE = re.compile(
    r"(?<![\w$])(?:&\s*)?f\s*(?:\.\s*(?P<dot_key>[\w$]+)|"
    r"\[\s*(['\"])(?P<bracket_key>[\w$]+)\2\s*\])"
)
_DISPLAY_ITEM_ARRAY_RE = re.compile(
    r"(?<![\w$])f\s*\.\s*(?P<array_name>Item|my_Item)\s*=\s*\[",
    re.IGNORECASE,
)
_DISPLAY_ITEM_FIELD_RE = re.compile(
    r"(?:[\"'](?P<quoted_key>name|memo)[\"']|(?P<bare_key>name|memo))\s*:\s*"
    r"(?:\"(?P<double>(?:\\.|[^\"\\])*)\"|"
    r"'(?P<single>(?:\\.|[^'\\])*)')",
    re.IGNORECASE,
)
# Japanese orthography that differs from the simplified forms used in Chinese.
_JAPANESE_FORM_CHARS = frozenset("薬剤絶長話踊髪歴気帰県楽変売読絵発継験桜粧")
_SCRIPT_START_TAGS = {"iscript", "html"}
_SCRIPT_END_TAGS = {"endscript", "endhtml"}
_VISIBLE_ATTRIBUTES = {
    "button": {"hint", "text"},
    "chara_new": {"jname"},
    "edit": {"placeholder"},
    "glink": {"text"},
    "ptext": {"text"},
    "savesnap": {"title"},
    "title": {"name"},
}
_CODE_PREFIX_RE = re.compile(
    r"^(?:f|sf|tf)\.|^(?:if|else|for|while|switch|function|var|let|const)\b|^\$\(|^TYRANO\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TyranoSpan:
    start: int
    end: int
    text: str
    line: int
    role: str
    speaker: str = ""
    quote: str = ""


@dataclass(frozen=True)
class _Tag:
    name: str
    start: int
    end: int
    attrs_start: int
    attrs_end: int


def decode_ks(data: bytes) -> tuple[str, str]:
    """Decode a Tyrano scenario while retaining the encoding for repack."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16" if data.startswith(b"\xff\xfe") else "utf-16-be"
        return data.decode(encoding), encoding
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def encode_ks(text: str, encoding: str) -> bytes:
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        # TyranoScript accepts UTF-8 scenario files; CP932 cannot represent CJK translations.
        return text.encode("utf-8-sig")


def contains_kana(text: str) -> bool:
    return bool(_KANA_RE.search(text or ""))


def is_japanese_visible_script(content: str) -> bool:
    """Classify a script from visible spans, excluding Japanese resource paths."""
    spans = [
        span for span in extract_spans(content, require_kana=False)
        if span.role != "speaker"
    ]
    if not spans:
        return False
    kana_count = sum(1 for span in spans if contains_kana(span.text))
    return kana_count >= 1 and kana_count / len(spans) >= 0.35


def extract_spans(content: str, *, require_kana: bool) -> list[TyranoSpan]:
    """Extract only visible text spans, never tag syntax or script expressions."""
    spans: list[TyranoSpan] = []
    referenced_variables = _collect_visible_variable_references(content)
    speaker = ""
    script_depth = 0
    display_item_depth = 0
    display_item_name = ""
    block_comment = False
    offset = 0

    for line_no, raw_line in enumerate(content.splitlines(keepends=True), start=1):
        body = raw_line.rstrip("\r\n")
        stripped = body.strip()
        line_start = offset
        offset += len(raw_line)

        # Tyrano recognizes /* and */ as block comments when on their own line.
        if stripped == "/*":
            block_comment = True
            continue
        if block_comment:
            if stripped == "*/":
                block_comment = False
            continue
        if not stripped or stripped.startswith((";", "*")):
            continue

        at_tag = _AT_TAG_RE.match(body)
        tags, incomplete_at = _scan_tags(body) if at_tag is None else ([], None)
        # Script content can itself contain strings such as "[endscript]".
        # While inside a block, only a standalone closing command ends it.
        if script_depth:
            script_ending = bool(
                (at_tag and at_tag.group(1).lower() in _SCRIPT_END_TAGS)
                or (
                    len(tags) == 1 and tags[0].name in _SCRIPT_END_TAGS
                    and not body[:tags[0].start].strip()
                    and not body[tags[0].end:].strip()
                )
            )
            if script_ending:
                display_item_depth = 0
                display_item_name = ""
            elif display_item_depth:
                _append_display_item_fields(
                    spans, content, body, line_start, line_no, require_kana,
                    display_item_name,
                )
                display_item_depth += _square_bracket_delta(body)
                if display_item_depth <= 0:
                    display_item_depth = 0
                    display_item_name = ""
            else:
                item_array = _DISPLAY_ITEM_ARRAY_RE.search(body)
                if item_array:
                    display_item_name = (
                        "Item" if item_array.group("array_name").casefold() == "item"
                        else "my_Item"
                    )
                    _append_display_item_fields(
                        spans, content, body, line_start, line_no, require_kana,
                        display_item_name,
                    )
                    display_item_depth = _square_bracket_delta(body[item_array.start():])
            # Display strings may be initialized inside [iscript]. Only
            # referenced variables, item display data, and Japanese name
            # defaults are eligible; arbitrary JavaScript stays excluded.
            _append_variable_assignment(
                spans, content, body, line_start, line_no, require_kana,
                referenced_variables,
            )
            if at_tag and at_tag.group(1).lower() in _SCRIPT_END_TAGS:
                script_depth = max(0, script_depth - 1)
            elif len(tags) == 1 and tags[0].name in _SCRIPT_END_TAGS and (
                not body[:tags[0].start].strip() and not body[tags[0].end:].strip()
            ):
                script_depth = max(0, script_depth - 1)
            continue

        tag_names = [at_tag.group(1).lower()] if at_tag else [tag.name for tag in tags]
        if any(name in _SCRIPT_START_TAGS for name in tag_names):
            script_depth = 1
            # An opening and closing tag on the same line must not hide the
            # rest of the scenario. Only accept a closing tag at line end.
            if _END_TAG_AT_LINE_END_RE.search(body):
                script_depth = 0
            continue
        if any(name in _SCRIPT_END_TAGS for name in tag_names):
            continue

        if stripped.startswith("#"):
            # #name[:face] is a chara_ptext reference, not necessarily display
            # text: translate only the visible name and keep the face suffix.
            name_start = len(body) - len(body.lstrip()) + 1
            name_end = body.find(":", name_start)
            if name_end < 0:
                name_end = len(body)
            raw_name = body[name_start:name_end]
            left = len(raw_name) - len(raw_name.lstrip())
            right = len(raw_name.rstrip())
            speaker = raw_name.strip()
            if speaker and not _is_dynamic_attribute(speaker):
                _append_span(
                    spans,
                    content,
                    line_start + name_start + left,
                    line_start + name_start + right,
                    line_no,
                    "speaker",
                    "",
                    require_kana,
                )
            continue

        if at_tag:
            _append_tag_attributes(
                spans, content, body, line_start, line_no,
                _Tag(at_tag.group(1).lower(), at_tag.start(), len(body), at_tag.end(), len(body)),
                speaker, require_kana,
            )
            continue

        if stripped.startswith("@"):
            continue

        _append_variable_assignment(
            spans, content, body, line_start, line_no, require_kana,
            referenced_variables,
        )

        if _looks_like_code(stripped):
            continue

        for tag in tags:
            _append_tag_attributes(
                spans, content, body, line_start, line_no, tag, speaker, require_kana,
            )

        cursor = 0
        for tag in tags:
            _append_visible_segment(
                spans,
                content,
                body,
                line_start,
                cursor,
                tag.start,
                line_no,
                speaker,
                require_kana,
            )
            cursor = tag.end
        _append_visible_segment(
            spans,
            content,
            body,
            line_start,
            cursor,
            incomplete_at if incomplete_at is not None else len(body),
            line_no,
            speaker,
            require_kana,
        )

    return _dedupe_spans(spans)


def _append_variable_assignment(
    spans: list[TyranoSpan], content: str, body: str, line_start: int,
    line_no: int, require_kana: bool, referenced_variables: set[str],
) -> None:
    for match in _VARIABLE_ASSIGNMENT_RE.finditer(body):
        key = match.group("dot_key") or match.group("bracket_key") or ""
        is_name_default = "name" in key.casefold()
        if not is_name_default and key not in referenced_variables:
            continue
        for literal_start, literal_end, value in _iter_assignment_string_literals(body, match.end()):
            # Escaped literals can have a different runtime value than their
            # source spelling, so leave them untouched rather than guessing.
            if "\\" in value or not _looks_like_display_literal(value):
                continue
            if (
                require_kana and is_name_default and not contains_kana(value)
                and not _contains_japanese_form(value)
            ):
                continue
            _append_span(
                spans, content, line_start + literal_start,
                line_start + literal_end, line_no, f"variable:{key}", "", False,
                quote=body[literal_start - 1],
            )


def _iter_assignment_string_literals(body: str, start: int):
    """Yield raw quoted literal contents from one JavaScript assignment RHS."""
    cursor = start
    quote = ""
    literal_start = -1
    escaped = False
    while cursor < len(body):
        char = body[cursor]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                yield literal_start, cursor, body[literal_start:cursor]
                quote = ""
        elif char in "\"'`":
            quote = char
            literal_start = cursor + 1
        elif char == ";":
            return
        cursor += 1


def _append_display_item_fields(
    spans: list[TyranoSpan], content: str, body: str, line_start: int,
    line_no: int, require_kana: bool, array_name: str,
) -> None:
    for match in _DISPLAY_ITEM_FIELD_RE.finditer(body):
        key = match.group("quoted_key") or match.group("bare_key") or ""
        value_group = "double" if match.group("double") is not None else "single"
        value = match.group(value_group)
        if "\\" in value or not _looks_like_display_literal(value):
            continue
        if require_kana and not contains_kana(value) and not _contains_japanese_form(value):
            continue
        start = line_start + match.start(value_group)
        _append_span(
            spans, content, start, line_start + match.end(value_group),
            line_no, f"variable:{array_name}.{key.casefold()}", "", False,
            quote='"' if value_group == "double" else "'",
        )


def _square_bracket_delta(source: str) -> int:
    """Count JavaScript array brackets while ignoring strings and escapes."""
    depth = 0
    quote = ""
    escaped = False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in "\"'`":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
    return depth


def _collect_visible_variable_references(content: str) -> set[str]:
    """Find f.* references used by visible scenario text or display attributes."""
    references: set[str] = set()
    script_depth = 0
    block_comment = False

    def add_references(value: str) -> None:
        for match in _VARIABLE_REFERENCE_RE.finditer(value):
            key = match.group("dot_key") or match.group("bracket_key")
            if key:
                references.add(key)

    def add_tag_references(body: str, tag: _Tag) -> None:
        allowed = _VISIBLE_ATTRIBUTES.get(tag.name, set())
        if tag.name == "emb":
            allowed = allowed | {"exp"}
        attrs = body[tag.attrs_start:tag.attrs_end]
        for attr in _ATTR_RE.finditer(attrs):
            if attr.group("name").lower() not in allowed:
                continue
            value = next(
                attr.group(group) for group in ("double", "single", "backtick", "unquoted")
                if attr.group(group) is not None
            )
            add_references(value)

    for raw_line in content.splitlines(keepends=True):
        body = raw_line.rstrip("\r\n")
        stripped = body.strip()
        if stripped == "/*":
            block_comment = True
            continue
        if block_comment:
            if stripped == "*/":
                block_comment = False
            continue
        if not stripped or stripped.startswith((";", "*")):
            continue

        at_tag = _AT_TAG_RE.match(body)
        tags, _incomplete_at = _scan_tags(body) if at_tag is None else ([], None)
        if script_depth:
            if at_tag and at_tag.group(1).lower() in _SCRIPT_END_TAGS:
                script_depth = max(0, script_depth - 1)
            elif len(tags) == 1 and tags[0].name in _SCRIPT_END_TAGS and (
                not body[:tags[0].start].strip() and not body[tags[0].end:].strip()
            ):
                script_depth = max(0, script_depth - 1)
            continue

        tag_names = [at_tag.group(1).lower()] if at_tag else [tag.name for tag in tags]
        if any(name in _SCRIPT_START_TAGS for name in tag_names):
            script_depth = 1
            if _END_TAG_AT_LINE_END_RE.search(body):
                script_depth = 0
            continue
        if any(name in _SCRIPT_END_TAGS for name in tag_names):
            continue

        if stripped.startswith("#"):
            add_references(stripped)
            continue
        if at_tag:
            add_tag_references(body, _Tag(
                at_tag.group(1).lower(), at_tag.start(), len(body), at_tag.end(), len(body),
            ))
            continue
        if stripped.startswith("@") or _looks_like_code(stripped):
            continue

        cursor = 0
        for tag in tags:
            add_references(body[cursor:tag.start])
            add_tag_references(body, tag)
            cursor = tag.end
        add_references(body[cursor:])

    return references


def _looks_like_display_literal(value: str) -> bool:
    text = str(value or "").strip()
    if not text or not (contains_kana(text) or re.search(r"[\u3400-\u9fff]", text)):
        return False
    if any(marker in text.casefold() for marker in ("/", "\\", ".png", ".jpg", ".ks", ".mp3", "http:")):
        return False
    return True


def _contains_japanese_form(value: str) -> bool:
    return any(char in _JAPANESE_FORM_CHARS for char in value)


def _scan_tags(body: str) -> tuple[list[_Tag], int | None]:
    """Locate bracket tags, respecting quoted and nested brackets."""
    tags: list[_Tag] = []
    cursor = 0
    while True:
        start = body.find("[", cursor)
        if start < 0:
            return tags, None
        name_start = start + 1
        while name_start < len(body) and body[name_start].isspace():
            name_start += 1
        name_match = _TAG_NAME_RE.match(body, name_start)
        if not name_match:
            # A malformed tag is not safe to treat as dialogue.
            return tags, start
        depth = 1
        quote = ""
        escaped = False
        pos = name_match.end()
        while pos < len(body):
            char = body[pos]
            if escaped:
                escaped = False
            elif quote and char == "\\":
                escaped = True
            elif quote:
                if char == quote:
                    quote = ""
            elif char in "\"'`":
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    tags.append(_Tag(
                        name_match.group().lower(), start, pos + 1, name_match.end(), pos,
                    ))
                    cursor = pos + 1
                    break
            pos += 1
        else:
            return tags, start


def _append_tag_attributes(
    spans: list[TyranoSpan], content: str, body: str, line_start: int,
    line_no: int, tag: _Tag, speaker: str, require_kana: bool,
) -> None:
    allowed = _VISIBLE_ATTRIBUTES.get(tag.name)
    if not allowed:
        return
    attrs = body[tag.attrs_start:tag.attrs_end]
    for attr in _ATTR_RE.finditer(attrs):
        name = attr.group("name").lower()
        if name not in allowed:
            continue
        value_group = next(
            group for group in ("double", "single", "backtick", "unquoted")
            if attr.group(group) is not None
        )
        quote = {"double": '"', "single": "'", "backtick": "`", "unquoted": ""}[value_group]
        start = line_start + tag.attrs_start + attr.start(value_group)
        end = line_start + tag.attrs_start + attr.end(value_group)
        value = content[start:end]
        field_requires_kana = require_kana and not (
            tag.name == "chara_new" and name == "jname"
            and _contains_japanese_form(value)
        )
        _append_span(
            spans, content, start, end, line_no, f"attribute:{tag.name}.{name}",
            speaker, field_requires_kana,
            quote=quote,
        )


def apply_span_translations(content: str, replacements: list[tuple[int, int, str, str]]) -> str:
    """Apply bound visible replacements without changing scenario syntax."""
    spans = {(span.start, span.end): span for span in extract_spans(content, require_kana=False)}
    updated = content
    for start, end, original, translated in sorted(replacements, key=lambda row: row[0], reverse=True):
        if start < 0 or end < start or end > len(updated):
            raise ValueError(f"Tyrano span out of range: {start}:{end}")
        if updated[start:end] != original:
            raise ValueError(f"Tyrano source span changed at {start}:{end}")
        span = spans.get((start, end))
        if span is None or span.text != original:
            raise ValueError(f"Tyrano replacement is not a visible span: {start}:{end}")
        translated = _safe_translation(content, span, translated)
        updated = updated[:start] + translated + updated[end:]
    return updated


def _safe_translation(content: str, span: TyranoSpan, translated: str) -> str:
    if any(char in translated for char in "\r\n\x00[]"):
        raise ValueError(f"Tyrano translation contains scenario syntax at {span.start}:{span.end}")
    if span.role == "speaker" and ":" in translated:
        raise ValueError(f"Tyrano speaker translation changes the face suffix at {span.start}:{span.end}")
    if span.role.startswith("variable:") and span.quote and span.quote in translated:
        raise ValueError(f"Tyrano variable translation breaks quoting at {span.start}:{span.end}")
    if span.role.startswith("attribute:"):
        if _is_dynamic_attribute(translated) or (span.quote and span.quote in translated):
            raise ValueError(f"Tyrano attribute translation breaks quoting at {span.start}:{span.end}")
        if not span.quote:
            # Bare values cannot contain whitespace or quote delimiters. Quote
            # them as a single value when doing so preserves the literal text.
            if any(char.isspace() or char in "\"'`" for char in translated):
                # Backticks preserve spaces even when the game's Tyrano
                # KeepSpaceInParameterValue setting strips quoted spaces.
                quote = next((char for char in ("`", '"', "'") if char not in translated), "")
                if any(char.isspace() for char in translated) and quote != "`":
                    raise ValueError(f"Tyrano attribute translation cannot preserve spaces at {span.start}:{span.end}")
                if not quote:
                    raise ValueError(f"Tyrano attribute translation cannot be safely quoted at {span.start}:{span.end}")
                return quote + translated + quote
        return translated
    line_start = content.rfind("\n", 0, span.start) + 1
    if not content[line_start:span.start].strip() and translated.lstrip().startswith(("@", "#", "*", ";", "/*")):
        raise ValueError(f"Tyrano translation would become a scenario command at {span.start}:{span.end}")
    return translated


def _append_visible_segment(
    spans: list[TyranoSpan],
    content: str,
    body: str,
    line_start: int,
    start: int,
    end: int,
    line_no: int,
    speaker: str,
    require_kana: bool,
) -> None:
    if end <= start:
        return
    cursor = start
    for match in _VARIABLE_REFERENCE_RE.finditer(body, start, end):
        _append_literal_segment(
            spans, content, body, line_start, cursor, match.start(),
            line_no, speaker, require_kana,
        )
        cursor = match.end()
    _append_literal_segment(
        spans, content, body, line_start, cursor, end,
        line_no, speaker, require_kana,
    )


def _append_literal_segment(
    spans: list[TyranoSpan], content: str, body: str, line_start: int,
    start: int, end: int, line_no: int, speaker: str, require_kana: bool,
) -> None:
    if end <= start:
        return
    segment = body[start:end]
    left = len(segment) - len(segment.lstrip())
    right = len(segment.rstrip())
    if right <= left:
        return
    _append_span(
        spans, content, line_start + start + left, line_start + start + right,
        line_no, "text", speaker, require_kana,
    )


def _append_span(
    spans: list[TyranoSpan],
    content: str,
    start: int,
    end: int,
    line_no: int,
    role: str,
    speaker: str,
    require_kana: bool,
    *,
    quote: str = "",
) -> None:
    text = content[start:end]
    if role.startswith("attribute:") and _is_dynamic_attribute(text):
        return
    if not is_translatable(text):
        return
    if require_kana and not contains_kana(text):
        return
    spans.append(TyranoSpan(start, end, text, line_no, role, speaker, quote))


def _is_dynamic_attribute(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped.startswith(("&", "%")) or "\\" in stripped:
        return True
    return bool(_VARIABLE_REFERENCE_RE.search(stripped))


def _looks_like_code(stripped: str) -> bool:
    if _CODE_PREFIX_RE.search(stripped):
        return True
    if re.match(r"^[A-Za-z_$][A-Za-z0-9_.$\[\]'\"]*\s*(?:=|\+=|-=|\+\+|--)", stripped):
        return True
    return False


def _dedupe_spans(spans: list[TyranoSpan]) -> list[TyranoSpan]:
    result: list[TyranoSpan] = []
    seen: set[tuple[int, int]] = set()
    for span in sorted(spans, key=lambda value: (value.start, value.end)):
        key = (span.start, span.end)
        if key in seen:
            continue
        seen.add(key)
        result.append(span)
    return result
