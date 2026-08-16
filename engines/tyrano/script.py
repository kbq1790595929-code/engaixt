"""Structure-aware extraction and patching for TyranoScript ``.ks`` files."""

from __future__ import annotations

import re
from dataclasses import dataclass

from utils.text_extract import is_translatable


_KANA_RE = re.compile(r"[\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f]")
_TAG_RE = re.compile(r"\[([A-Za-z_][A-Za-z0-9_.:-]*)([^\]]*)\]")
_ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.:-]*)\s*=\s*([\"'])(.*?)\2")
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
    spans = extract_spans(content, require_kana=False)
    if not spans:
        return False
    kana_count = sum(1 for span in spans if contains_kana(span.text))
    return kana_count >= 1 and kana_count / len(spans) >= 0.35


def extract_spans(content: str, *, require_kana: bool) -> list[TyranoSpan]:
    """Extract only visible text spans, never tag syntax or script expressions."""
    spans: list[TyranoSpan] = []
    speaker = ""
    script_depth = 0
    offset = 0

    for line_no, raw_line in enumerate(content.splitlines(keepends=True), start=1):
        body = raw_line.rstrip("\r\n")
        stripped = body.strip()
        line_start = offset
        offset += len(raw_line)

        tags = list(_TAG_RE.finditer(body))
        lower_tags = {match.group(1).lower() for match in tags}
        if script_depth:
            if lower_tags & _SCRIPT_END_TAGS:
                script_depth = max(0, script_depth - 1)
            continue
        if lower_tags & _SCRIPT_START_TAGS:
            script_depth += 1
            continue
        if not stripped or stripped.startswith((";", "*", "@")):
            continue
        if _looks_like_code(stripped):
            continue

        if stripped.startswith("#") and not re.fullmatch(r"#[0-9A-Fa-f]{3,8}", stripped):
            value = stripped[1:].strip()
            value_start = body.find(value, body.find("#") + 1)
            if value and value_start >= 0:
                speaker = value
                _append_span(
                    spans,
                    content,
                    line_start + value_start,
                    line_start + value_start + len(value),
                    line_no,
                    "speaker",
                    speaker,
                    require_kana,
                )
            continue

        for tag in tags:
            tag_name = tag.group(1).lower()
            allowed = _VISIBLE_ATTRIBUTES.get(tag_name)
            if not allowed:
                continue
            attrs_text = tag.group(2)
            attrs_base = line_start + tag.start(2)
            for attr in _ATTR_RE.finditer(attrs_text):
                if attr.group(1).lower() not in allowed:
                    continue
                start = attrs_base + attr.start(3)
                end = attrs_base + attr.end(3)
                _append_span(
                    spans,
                    content,
                    start,
                    end,
                    line_no,
                    f"attribute:{tag_name}.{attr.group(1).lower()}",
                    speaker,
                    require_kana,
                )

        cursor = 0
        for tag in tags:
            _append_visible_segment(
                spans,
                content,
                body,
                line_start,
                cursor,
                tag.start(),
                line_no,
                speaker,
                require_kana,
            )
            cursor = tag.end()
        _append_visible_segment(
            spans,
            content,
            body,
            line_start,
            cursor,
            len(body),
            line_no,
            speaker,
            require_kana,
        )

    return _dedupe_spans(spans)


def apply_span_translations(content: str, replacements: list[tuple[int, int, str, str]]) -> str:
    """Apply exact-span replacements in reverse order and verify source binding."""
    updated = content
    for start, end, original, translated in sorted(replacements, key=lambda row: row[0], reverse=True):
        if start < 0 or end < start or end > len(updated):
            raise ValueError(f"Tyrano span out of range: {start}:{end}")
        if updated[start:end] != original:
            raise ValueError(f"Tyrano source span changed at {start}:{end}")
        updated = updated[:start] + translated + updated[end:]
    return updated


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
    segment = body[start:end]
    left = len(segment) - len(segment.lstrip())
    right = len(segment.rstrip())
    if right <= left:
        return
    _append_span(
        spans,
        content,
        line_start + start + left,
        line_start + start + right,
        line_no,
        "text",
        speaker,
        require_kana,
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
) -> None:
    text = content[start:end]
    if role.startswith("attribute:") and _is_dynamic_attribute(text):
        return
    if not is_translatable(text):
        return
    if require_kana and not contains_kana(text):
        return
    spans.append(TyranoSpan(start, end, text, line_no, role, speaker))


def _is_dynamic_attribute(text: str) -> bool:
    stripped = (text or "").strip()
    if stripped.startswith("&"):
        return True
    return bool(re.search(r"(?:^|[^A-Za-z0-9_])(?:f|sf|tf)\.[A-Za-z_]", stripped))


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
