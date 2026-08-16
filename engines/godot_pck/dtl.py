"""Dialogic DTL 时间线文本提取与回填。"""

from __future__ import annotations

import re

from engines.base import TextItem
from utils.text_extract import is_translatable


# ---------------------------------------------------------------------------
# DTL text extraction
# ---------------------------------------------------------------------------

_DTL_SKIP_RE = re.compile(r'^(do |audio |join |leave |update |set |label |jump |\[|# )', re.IGNORECASE)
_DTL_TEXT_INPUT_DEFAULT_RE = re.compile(r'default="((?:[^"\\]|\\.)*)"')
_DTL_BRACKET_TAG_RE = re.compile(r'^\[/?([A-Za-z_][\w.-]*)(?:[=\s][^\]]*)?\](.*)$')
_DTL_VISIBLE_FORMAT_TAGS = {
    "b", "i", "u", "s", "font", "color", "bgcolor", "outline",
    "center", "left", "right", "fill", "indent", "url",
    "speed", "wave", "shake", "rainbow", "pulse", "fade", "code",
}
_DTL_BBCODE_TAG_RE = re.compile(r"\[/?[A-Za-z_][^\]]*\]")


def _unescape_godot_quoted(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == '"':
                result.append('"')
            elif nxt == '\\':
                result.append('\\')
            elif nxt == 'n':
                result.append('\n')
            elif nxt == 't':
                result.append('\t')
            else:
                result.append(s[i:i + 2])
            i += 2
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


def _escape_godot_quoted(s: str) -> str:
    out = []
    for c in s:
        if c == '\\':
            out.append('\\\\')
        elif c == '"':
            out.append('\\"')
        elif c == '\n':
            out.append('\\n')
        elif c == '\t':
            out.append('\\t')
        else:
            out.append(c)
    return ''.join(out)


def _dtl_text_input_default(stripped: str) -> str | None:
    if not stripped.startswith("[text_input "):
        return None
    match = _DTL_TEXT_INPUT_DEFAULT_RE.search(stripped)
    if not match:
        return None
    return _unescape_godot_quoted(match.group(1))


def _dtl_bracket_line_is_visible_text(stripped: str) -> bool:
    if not stripped.startswith("[") or stripped.startswith("[text_input "):
        return False
    match = _DTL_BRACKET_TAG_RE.match(stripped)
    if not match:
        return False
    tag = match.group(1).lower()
    tail = match.group(2).strip()
    visible = _DTL_BBCODE_TAG_RE.sub("", stripped).strip()
    has_language = bool(re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", visible))
    if tag in _DTL_VISIBLE_FORMAT_TAGS and tail:
        return has_language
    return bool(tail and has_language)


def _collect_dtl_continuation(lines: list[str], start: int, initial: str) -> tuple[str, int]:
    """Collect Dialogic backslash continuations into one logical text block."""
    full = initial
    i = start
    while full.endswith("\\") and i + 1 < len(lines):
        i += 1
        full = full[:-1] + lines[i].strip()
    return full, i


def _previous_visible_dtl_text(lines: list[str], start: int) -> str:
    for i in range(start - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or _DTL_SKIP_RE.match(stripped):
            continue
        text, _ = _collect_dtl_continuation(lines, i, stripped)
        return text
    return ""


def _choice_following_commands(lines: list[str], start: int) -> list[str]:
    commands: list[str] = []
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if not line.startswith(("\t", "    ")):
            break
        stripped = line.strip()
        if stripped:
            commands.append(stripped)
        i += 1
    return commands


def _contextualize_dtl_choice_translation(lines: list[str], index: int, original: str, translated: str) -> str:
    """Make dangerous yes/no choices explicit when their branch changes flow."""
    if not original.startswith("- "):
        return translated

    choice_text = original[2:].strip()
    question = _previous_visible_dtl_text(lines, index)
    commands = _choice_following_commands(lines, index)

    skip_question = bool(re.search(r"(跳过|跳過|スキップ|skip)", question, re.IGNORECASE))
    jumps_to_skip = any(re.search(r"\bjump\s+skip\b", cmd, re.IGNORECASE) for cmd in commands)
    if not skip_question:
        return translated

    if jumps_to_skip:
        return "- 跳过剧情" if translated.startswith("- ") else "跳过剧情"

    if choice_text in {"いいえ", "いえ", "否", "no", "No", "NO"}:
        return "- 观看剧情" if translated.startswith("- ") else "观看剧情"

    return translated


def extract_dtl_text(content: str, rel_path: str) -> list[TextItem]:
    """Extract translatable text from a Dialogic Timeline (.dtl) file."""
    items = []
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Skip empty, commands, labels, joins, audio, signals
        if not stripped:
            i += 1
            continue

        default_value = _dtl_text_input_default(stripped)
        if default_value is not None:
            if is_translatable(default_value):
                items.append(TextItem(
                    file=rel_path,
                    key=f"input_{i}",
                    original=default_value,
                    line=i + 1,
                    meta={"dtl_command": "text_input_default"},
                ))
            i += 1
            continue

        if _DTL_SKIP_RE.match(stripped) and not _dtl_bracket_line_is_visible_text(stripped):
            i += 1
            continue

        # Skip # comment lines (but extract translatable comments)
        if stripped.startswith("#") and not stripped.startswith("# "):
            i += 1
            continue

        # Choice option: "- text"
        if stripped.startswith("- ") and len(stripped) > 2:
            opt_text = stripped[2:].strip()
            if is_translatable(opt_text):
                items.append(TextItem(
                    file=rel_path, key=f"choice_{i}", original=stripped,
                    line=i + 1,
                ))
            i += 1
            continue

        # Dialogue: "Name: text" (with optional \ continuation)
        if ":" in stripped:
            colon_idx = stripped.index(":")
            name = stripped[:colon_idx]
            # Valid speaker names: alphanumeric, no spaces, not starting with http
            if name and " " not in name and not name.startswith("http"):
                text = stripped[colon_idx + 1:].strip()
                # Collect continuation lines (\)
                full_text, i = _collect_dtl_continuation(lines, i, text)
                if full_text and is_translatable(full_text):
                    items.append(TextItem(
                        file=rel_path, key=f"dlg_{i}", original=full_text,
                        line=i + 1,
                    ))
                i += 1
                continue

        # Standalone text line (narration), including Dialogic "\" continuations.
        full_text, end_i = _collect_dtl_continuation(lines, i, stripped)
        if is_translatable(full_text) or _dtl_bracket_line_is_visible_text(full_text):
            items.append(TextItem(
                file=rel_path, key=f"text_{end_i}", original=full_text,
                line=end_i + 1,
            ))
            i = end_i

        i += 1

    return items


# ---------------------------------------------------------------------------
# DTL content patching
# ---------------------------------------------------------------------------

def patch_dtl_content(content: str, translations: dict[str, str]) -> tuple[str, int]:
    """Apply translations to DTL content. Returns (patched_content, replace_count)."""
    lines = content.split("\n")
    replaced = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        # text_input default value
        if stripped.startswith("[text_input "):
            match = _DTL_TEXT_INPUT_DEFAULT_RE.search(stripped)
            if not match:
                continue
            default_val = _unescape_godot_quoted(match.group(1))
            if default_val in translations:
                before = stripped[:match.start(1)]
                after = stripped[match.end(1):]
                new_line = before + _escape_godot_quoted(translations[default_val]) + after
                if new_line != stripped:
                    indent = line[:len(line) - len(line.lstrip())]
                    lines[i] = indent + new_line
                    replaced += 1
            continue

        # Dialogue: "Name: text" (with optional \ continuation)
        if ":" in stripped:
            colon_idx = stripped.index(":")
            name = stripped[:colon_idx]
            text = stripped[colon_idx + 1:]
            if name and " " not in name and not name.startswith("http"):
                trimmed = text.strip()
                # Collect full text with continuations, matching extraction logic
                full, ci = _collect_dtl_continuation(lines, i, trimmed)
                indent = line[:len(line) - len(line.lstrip())]
                if full in translations:
                    new_line = indent + name + ": " + translations[full]
                    if new_line != line:
                        lines[i] = new_line
                        replaced += 1
                    # Clear continuation lines
                    for cl in range(i + 1, ci + 1):
                        if cl < len(lines):
                            lines[cl] = ""
                elif trimmed in translations:
                    # Fallback: match first line only (backward compat)
                    new_line = indent + name + ": " + translations[trimmed]
                    if new_line != line:
                        lines[i] = new_line
                        replaced += 1
                continue

        # Standalone narration with "\" continuations.
        if not stripped.startswith("- ") and not stripped.startswith("#"):
            full, ci = _collect_dtl_continuation(lines, i, stripped)
            if ci > i and full in translations:
                indent = line[:len(line) - len(line.lstrip())]
                new_line = indent + translations[full]
                if new_line != line:
                    lines[i] = new_line
                    replaced += 1
                for cl in range(i + 1, ci + 1):
                    if cl < len(lines):
                        lines[cl] = ""
                continue

        # Full-line match (choices, standalone text, comments)
        if stripped in translations:
            indent = line[:len(line) - len(line.lstrip())]
            trans = translations[stripped]
            trans = _contextualize_dtl_choice_translation(lines, i, stripped, trans)
            # Preserve DTL choice prefix
            if stripped.startswith("- ") and not trans.startswith("- "):
                trans = "- " + trans
            new_line = indent + trans
            if new_line != line:
                lines[i] = new_line
                replaced += 1
            continue

        # Comment line: "# text"
        if stripped.startswith("#") and stripped not in translations:
            comment_text = stripped[1:].strip()
            if comment_text and comment_text in translations:
                indent = line[:len(line) - len(line.lstrip())]
                new_line = indent + "# " + translations[comment_text]
                if new_line != line:
                    lines[i] = new_line
                    replaced += 1

    return "\n".join(lines), replaced
