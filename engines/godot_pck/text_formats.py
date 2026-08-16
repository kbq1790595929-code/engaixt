"""TSCN/TRES 字符串、dialogue txt 与 SCN/RES 二进制内联字符串的提取与安全回填。"""

from __future__ import annotations

import re
import struct

from engines.base import TextItem
from utils.text_extract import extract_placeholders, is_translatable

from engines.godot_pck.dtl import _escape_godot_quoted, _unescape_godot_quoted


# ---------------------------------------------------------------------------
# TSCN / TRES text resource string extraction
# ---------------------------------------------------------------------------

_SKIP_TSCN_PROPS = {
    "uid", "id", "name", "type", "parent", "path", "script", "load_steps", "format",
    "timeline", "next_name", "resource_name", "resource_path", "identifier",
    "bus", "stream", "volume_db", "size_flags_horizontal", "size_flags_vertical",
    "offset_left", "offset_top", "offset_right", "offset_bottom",
    "anchor_left", "anchor_top", "anchor_right", "anchor_bottom",
    "grow_horizontal", "grow_vertical", "layout_mode",
    "pivot_offset", "position", "rotation", "scale", "size",
    "metadata", "editor_description",
}


def _unescape_tscn(s: str) -> str:
    return _unescape_godot_quoted(s)


def _escape_tscn(s: str) -> str:
    return _escape_godot_quoted(s)


def extract_tscn_strings(content: str, rel_path: str) -> list[TextItem]:
    """Extract translatable strings from Godot text scenes/resources (.tscn/.tres)."""
    items = []
    for line_no, line in enumerate(content.split("\n"), 1):
        m = re.match(r'^(\s*)(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"', line)
        if not m:
            continue
        prop = m.group(2)
        raw = m.group(3)
        if prop in _SKIP_TSCN_PROPS:
            continue
        value = _unescape_tscn(raw)
        if _is_godot_runtime_identifier(value):
            continue
        if not is_translatable(value):
            continue
        items.append(TextItem(file=rel_path, key=f"L{line_no}:{prop}", original=value))
    return items


def patch_tscn_strings(content: str, translations: dict[str, str]) -> tuple[str, int]:
    """Apply translations to Godot text scenes/resources. Returns (content, count)."""
    lines = content.split("\n")
    replaced = 0
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"', line)
        if not m:
            continue
        indent, prop, raw = m.groups()
        if prop in _SKIP_TSCN_PROPS:
            continue
        value = _unescape_tscn(raw)
        if _is_godot_runtime_identifier(value):
            continue
        if value not in translations:
            continue
        trans = translations[value]
        if trans == value:
            continue
        escaped = _escape_tscn(trans)
        lines[i] = f'{indent}{prop} = "{escaped}"'
        replaced += 1
    return "\n".join(lines), replaced


# ---------------------------------------------------------------------------
# Custom dialogue .txt extraction (GodotSteam dialogue format)
# Format: [narration?] [speaker\n dialogue\n] pairs, null bytes as separators
# ---------------------------------------------------------------------------

def extract_dialogue_txt(content: str, rel_path: str) -> list[TextItem]:
    """Extract translatable text from custom dialogue .txt files.

    Format: optional narration line, then alternating speaker/dialogue pairs.
    After the first line (narration), odd-indexed lines are speakers, even are dialogue.
    """
    items = []
    clean = content.replace("\x00", "")
    lines = [l.strip() for l in clean.split("\n")]
    lines = [l for l in lines if l]

    if not lines:
        return items

    from collections import Counter
    freq = Counter(lines)
    known_speakers = {l for l, c in freq.items() if c >= 2}

    # Determine if first line is narration or first speaker
    # First line is narration if it appears only once AND looks like a sentence
    first_is_narration = False
    if lines[0] not in known_speakers:
        s = lines[0]
        if len(s) > 25 or " " in s or s.rstrip().endswith((".", "!", "?")):
            first_is_narration = True

    for i, line in enumerate(lines):
        if i == 0 and first_is_narration:
            pass  # extract narration
        elif i == 0:
            continue  # first line is a speaker, skip
        elif first_is_narration:
            # After narration: odd i = speaker, even i = dialogue
            if i % 2 == 1:  # speaker
                known_speakers.add(line)
                continue
        else:
            # No narration: even i = speaker, odd i = dialogue
            if i % 2 == 0:  # speaker
                known_speakers.add(line)
                continue

        if not is_translatable(line):
            continue
        items.append(TextItem(
            file=rel_path, key=f"line_{i}", original=line, line=i + 1,
        ))

    return items


def patch_dialogue_txt(content: str, translations: dict[str, str]) -> tuple[str, int]:
    """Apply translations to custom dialogue .txt content.

    Uses the same format detection as extract_dialogue_txt.
    """
    clean = content.replace("\x00", "")
    lines = clean.split("\n")
    replaced = 0

    from collections import Counter
    stripped_lines = [l.strip() for l in lines if l.strip()]
    if not stripped_lines:
        return content, 0

    freq = Counter(stripped_lines)
    known_speakers = {l for l, c in freq.items() if c >= 2}

    first_is_narration = False
    if stripped_lines[0] not in known_speakers:
        s = stripped_lines[0]
        if len(s) > 25 or " " in s or s.rstrip().endswith((".", "!", "?")):
            first_is_narration = True

    # Build a set of translatable line indices
    translatable_indices = set()
    for i, line in enumerate(stripped_lines):
        if i == 0 and first_is_narration:
            translatable_indices.add(i)
        elif i == 0:
            continue
        elif first_is_narration:
            if i % 2 == 0:  # dialogue (even after narration)
                translatable_indices.add(i)
        else:
            if i % 2 == 1:  # dialogue (odd when no narration)
                translatable_indices.add(i)

    # Apply translations
    new_lines = []
    si = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or si not in translatable_indices:
            new_lines.append(line)
            if stripped:
                si += 1
            continue

        si += 1
        if stripped in translations:
            trans = translations[stripped]
            if trans != stripped:
                lead = line[:len(line) - len(line.lstrip())]
                new_lines.append(lead + trans)
                replaced += 1
                continue

        new_lines.append(line)

    return "\n".join(new_lines), replaced


# ---------------------------------------------------------------------------
# SCN/RES binary safe string replacement
# ---------------------------------------------------------------------------

_WRAPPING_QUOTES = "\"'“”‘’「」『』"
_DROP_PUNCT = "，。！？、；：,.!?;:“”‘’「」『』（）()《》【】[]…"
_PHRASE_SHORTENERS = (
    ("看起来", ""),
    ("的样子", ""),
    ("有点", "稍"),
    ("不太", "不"),
    ("人家", "我"),
    ("再给我多一点", "再多点"),
    ("多一点", "多点"),
    ("这种", ""),
    ("口感", ""),
    ("超爱", "爱"),
    ("喜欢", "喜"),
    ("隐藏", "藏"),
    ("打开", "开"),
    ("关闭", "关"),
    ("编辑", "改"),
    ("选择", "选"),
    ("默认", "预设"),
    ("全部", "全"),
    ("替换", "换"),
    ("搜索", "查"),
    ("文本", "文"),
    ("场景", "景"),
    ("事件", "事"),
    ("快捷键", "热键"),
)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="strict"))


def _strip_wrapping_quotes(text: str) -> str:
    stripped = text.strip()
    while len(stripped) >= 2 and stripped[0] in _WRAPPING_QUOTES and stripped[-1] in _WRAPPING_QUOTES:
        stripped = stripped[1:-1].strip()
    return stripped


def _is_placeholder_safe(original: str, candidate: str) -> bool:
    placeholders = set(extract_placeholders(original))
    return not placeholders or placeholders.issubset(set(extract_placeholders(candidate)))


def _fit_translation_to_utf8_slot(original: str, translated: str, byte_limit: int) -> str | None:
    """Best-effort shortening for fixed-size binary Godot string slots."""
    if not translated or translated == original:
        return None

    candidates: list[str] = []

    def add(text: str | None):
        if not text:
            return
        text = text.strip()
        if text and text not in candidates and _is_placeholder_safe(original, text):
            candidates.append(text)

    add(translated)
    add(_strip_wrapping_quotes(translated))
    add(_strip_wrapping_quotes(translated).replace("……", "…"))
    add(_strip_wrapping_quotes(translated).replace("……", "").replace("...", ""))
    add(_strip_wrapping_quotes(translated).translate(str.maketrans("", "", _DROP_PUNCT)))

    compact = _strip_wrapping_quotes(translated)
    for old, new in _PHRASE_SHORTENERS:
        compact = compact.replace(old, new)
    add(compact)
    add(compact.translate(str.maketrans("", "", _DROP_PUNCT)))

    for candidate in candidates:
        if _utf8_len(candidate) <= byte_limit:
            return candidate

    no_ph = not extract_placeholders(original)
    compact = candidates[-1] if candidates else translated.strip()
    if no_ph:
        # Last resort: character-boundary truncation. One Chinese character is
        # three UTF-8 bytes, so this is often enough for short UI/onomatopoeia.
        out = ""
        for ch in compact:
            if _utf8_len(out + ch) > byte_limit:
                break
            out += ch
        if out and out != original and _utf8_len(out) <= byte_limit:
            return out

    return None


_GODOT_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+$")

# Built-in Godot 4.x class/type-tag names that can appear as bare
# VARIANT_STRING values inside binary SCN/RES resources (e.g. the
# ext_resource/sub_resource "type" field, or a Resource's script class
# name). These are single common-English-looking words that otherwise
# pass the natural-language heuristics in is_translatable(), so without
# this list they get extracted and translated like normal dialogue text.
# Patching them corrupts the resource's type tag (e.g. "Script" ->
# "脚本"), which makes Godot's ResourceLoader fail to load the resource
# ("No loader found for resource ... expected type: 脚本") and can leave
# a dependent scene/UI layer null, surfacing as a gray screen at runtime.
# This is intentionally scoped to the binary SCN/RES scanner only: the
# text-format TSCN/TRES path already excludes the "type"/"script" keys
# by property name via _SKIP_TSCN_PROPS.
_GODOT_BUILTIN_TYPE_NAMES = frozenset({
    "Script", "GDScript", "CSharpScript", "Resource", "PackedScene",
    "Node", "Node2D", "Node3D", "Control", "CanvasItem", "CanvasLayer",
    "Label", "Button", "RichTextLabel", "TextEdit", "LineEdit",
    "Timer", "Tween", "Font", "FontFile", "FontVariation",
    "Texture2D", "Texture", "Gradient", "Curve", "Curve2D", "Curve3D",
    "StyleBox", "StyleBoxFlat", "StyleBoxEmpty", "StyleBoxTexture",
    "Theme", "ShaderMaterial", "Shader", "Material", "BaseMaterial3D",
    "AnimationPlayer", "Animation", "AnimationTree", "AnimatedSprite2D",
    "Sprite2D", "Sprite3D", "TextureRect", "ColorRect", "NinePatchRect",
    "VBoxContainer", "HBoxContainer", "GridContainer", "PanelContainer",
    "MarginContainer", "CenterContainer", "ScrollContainer",
    "SubViewport", "SubViewportContainer", "Viewport", "Camera2D", "Camera3D",
    "AudioStreamPlayer", "AudioStreamPlayer2D", "AudioStreamPlayer3D",
    "AudioStream", "InputEventKey", "InputEventMouseButton", "InputEvent",
    "StaticBody2D", "CharacterBody2D", "RigidBody2D", "Area2D",
    "CollisionShape2D", "CollisionPolygon2D", "Panel", "PopupMenu",
    "WindowDialog", "Window", "ConfirmationDialog", "AcceptDialog",
    "OptionButton", "CheckBox", "CheckButton", "TabContainer", "Tabs",
    "ItemList", "Tree", "TreeItem", "ProgressBar", "Slider", "HSlider",
    "VSlider", "SpinBox", "Container", "Object", "RefCounted",
})


def _is_godot_runtime_identifier(value: str) -> bool:
    """Return true for Godot resource/timeline IDs that must not be translated."""
    s = value.strip()
    if not s:
        return False
    if not _GODOT_RUNTIME_ID_RE.match(s):
        return False
    if not re.search(r"[A-Za-z]", s):
        return False
    # Natural language in exported resources should use spaces or CJK/kana,
    # while Godot timeline/event/resource IDs are ASCII tokens with underscores.
    return all(ord(ch) < 128 for ch in s)


def patch_inline_strings_safe(data: bytes, translations: dict[str, str],
                              allow_longer: bool = False) -> tuple[bytes, int, int, int]:
    """Replace VARIANT_STRING values in binary Godot resources (SCN/RES).

    When allow_longer=False (default, for SCN/RES): longer translations are skipped,
    shorter translations are padded with spaces to maintain exact byte length,
    preventing any byte shifts that would corrupt internal binary offsets.
    When allow_longer=True (for DTL/timeline files): variable-length replacement is allowed.
    Returns (patched_data, replaced_shorter, replaced_longer, replaced_equal).
    """
    result = bytearray(data)
    operations = []

    for original, translated in translations.items():
        if original == translated:
            continue
        if _is_godot_runtime_identifier(original):
            continue
        if original in _GODOT_BUILTIN_TYPE_NAMES:
            continue
        orig_bytes = original.encode("utf-8")
        byte_limit = len(orig_bytes)
        if not allow_longer:
            fitted = _fit_translation_to_utf8_slot(original, translated, byte_limit)
            if not fitted:
                continue
            translated = fitted
        trans_bytes = translated.encode("utf-8")
        old_stored = len(orig_bytes) + 1
        new_stored = len(trans_bytes) + 1

        # SCN/RES binary safety: keep exact byte length to preserve internal offsets
        if not allow_longer and new_stored != old_stored:
            if new_stored > old_stored:
                continue
            # Shorter: pad with spaces to maintain exact byte length
            padding_needed = old_stored - new_stored
            trans_bytes += b" " * padding_needed
            new_stored = old_stored

        search_pos = 0
        while True:
            idx = result.find(orig_bytes, search_pos)
            if idx == -1:
                break
            if idx >= 8 and idx + old_stored <= len(result):
                var_type = struct.unpack_from("<I", result, idx - 8)[0]
                stored_len = struct.unpack_from("<I", result, idx - 4)[0]
                # VARIANT_STRING (type=5): [05 00 00 00][stored_len:u32][UTF-8:N][\x00]
                if var_type == 5 and stored_len == old_stored and result[idx + len(orig_bytes)] == 0:
                    var_start = idx - 8
                    new_data = trans_bytes + b"\x00"
                    operations.append((var_start, old_stored, new_stored, new_data))
            search_pos = idx + 1

    if not operations:
        return bytes(result), 0, 0, 0

    operations.sort(key=lambda x: x[0], reverse=True)
    replaced_shorter = 0
    replaced_longer = 0
    replaced_equal = 0
    for var_start, old_stored, new_stored, new_data in operations:
        old_block_end = var_start + 8 + old_stored
        new_block_end = var_start + 8 + new_stored
        delta = new_stored - old_stored
        if delta < 0:
            del result[new_block_end:old_block_end]
            replaced_shorter += 1
        elif delta > 0:
            result[old_block_end:old_block_end] = b"\x00" * delta
            replaced_longer += 1
        else:
            replaced_equal += 1
        struct.pack_into("<I", result, var_start + 4, new_stored)
        result[var_start + 8:var_start + 8 + new_stored] = new_data

    return bytes(result), replaced_shorter, replaced_longer, replaced_equal
