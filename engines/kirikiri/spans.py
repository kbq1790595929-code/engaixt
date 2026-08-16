"""KiriKiri KAG/TJS 脚本文本 span 提取与脚本/文本分类判定。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from engines.base import TextItem
from utils.text_extract import is_translatable


@dataclass(frozen=True)
class _TextSpan:
    text: str
    start: int
    end: int


_TJS_CODE_STARTS = re.compile(
    r"^\s*(var|let|const|function|class|if|for|while|switch|return|new|delete|typeof|"
    r"throw|try|catch|finally|break|continue|export|import|global|property|"
    r"static|public|private|protected|async|await|yield|super|extends|implements|"
    r"interface|enum|abstract)\b"
)
_TJS_ASSIGNMENT = re.compile(r"^\s*[A-Za-z_$][\w.$]*\s*[+\-*/%]?=\s*")
_TJS_LINE_COMMENT = re.compile(r"^\s*//")
_QUOTED_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'')
_KAG_COMMAND_TEXT_ATTR_RE = re.compile(r'\b(?:text|caption|name)\s*=\s*["\']([^"\']+)["\']', re.I)
_KAG_SAFE_TEXT_ATTR_COMMANDS = {"button", "glink", "link"}
_KAG_BRACKET_MESSAGE_RE = re.compile(r"^(\[[^\]]+\])(.+?)(\[[A-Za-z_<][^\]]*\].*)$")
_TJS_TEXT_CONTEXT_RE = re.compile(
    r"\b(?:caption|title|text|message|msg|label|name|menu|inform|confirm|prompt|drawText|originalCh|tooltip|hint)\b",
    re.I,
)
_COMMON_FONT_NAMES = {
    "ms gothic",
    "ms ui gothic",
    "ms mincho",
    "meiryo",
    "ｍｓ ゴシック",
    "ｍｓ 明朝",
    "メイリオ",
}


def _allow_plain_kag_script(rel: str) -> bool:
    normalized = rel.replace("\\", "/").lower()
    if normalized.startswith("scenario/"):
        return True
    if not normalized.startswith("system/"):
        return True
    from engines.kirikiri.engine import KiriKiriEngine

    return bool(KiriKiriEngine._SYSTEM_KS_ALLOW_RE.search(normalized))


def _is_kirikiri_control_text_item(item: TextItem) -> bool:
    original = (getattr(item, "original", "") or "").strip()
    if _looks_like_kirikiri_control_identifier(original):
        return True
    if getattr(item, "translated", "") and _looks_like_kirikiri_control_identifier(str(item.translated).strip()):
        return True
    return False


_KIRIKIRI_SAFE_SCRIPT_NAMES = {
    "ed.ks",
    "ending.ks",
    "scenario.ks",
    "scr.ks",
}


_KIRIKIRI_SYSTEM_SCRIPT_LEAVES = {
    "about.ks",
    "buttonlinkplugin.ks",
    "config.ks",
    "exsystembutton.ks",
    "inputname_z.ks",
    "macro.ks",
    "macro_bu.ks",
    "oldmovie.ks",
    "quake2.ks",
    "save.ks",
    "set.ks",
    "setup.ks",
    "show_layer_window.ks",
    "skn_slider.ks",
    "windowzoom.ks",
}

_KIRIKIRI_SYSTEM_SCRIPT_PREFIXES = (
    "button",
    "config",
    "exsystem",
    "inputname",
    "macro",
    "plugin",
    "save",
    "setup",
    "show_layer",
    "skn_",
    "system",
    "window",
)


def _is_kirikiri_flat_scenario_script(rel: str) -> bool:
    leaf = rel.rsplit("/", 1)[-1].lower()
    return bool(re.fullmatch(r"\d{1,3}(?:[_-]\d{1,3})+\.ks", leaf))


def _is_kirikiri_system_or_logic_script(rel: str) -> bool:
    lowered = str(rel or "").replace("\\", "/").lstrip("/").lower()
    if not lowered:
        return True
    if lowered.startswith(("system/", "plugin_ks/", "config/")):
        return True
    leaf = lowered.rsplit("/", 1)[-1]
    if leaf.startswith("startup.") or lowered in {"startup.ks", "startup.tjs"}:
        return True
    if leaf in _KIRIKIRI_SYSTEM_SCRIPT_LEAVES:
        return True
    stem = leaf.rsplit(".", 1)[0]
    return stem.startswith(_KIRIKIRI_SYSTEM_SCRIPT_PREFIXES)


def _is_kirikiri_scenario_archive_name(name: str) -> bool:
    lower = Path(str(name or "").replace("\\", "/")).name.lower()
    stem = Path(lower).stem
    if lower in {"data.xp3", "scn.xp3", "scenario.xp3", "script.xp3", "scripts.xp3"}:
        return True
    return (
        "scenario" in stem
        or stem in {"scn", "script", "scripts", "scenario_adv", "scenario_common"}
        or stem.endswith("_scn")
        or stem.endswith("scn")
        or stem.startswith("scn")
        or "script" in stem
    )


def _is_kirikiri_safe_default_repack_file(file_name: str) -> bool:
    rel = str(file_name or "").replace("\\", "/").lstrip("/").lower()
    if not rel:
        return False
    if rel.startswith("scenario/"):
        leaf = rel.rsplit("/", 1)[-1]
        return leaf not in {"_first.ks", "scr.ks"} and not leaf.startswith("_")
    if _is_kirikiri_system_or_logic_script(rel):
        return False
    if _is_kirikiri_flat_scenario_script(rel):
        return True
    leaf = rel.rsplit("/", 1)[-1]
    if leaf in _KIRIKIRI_SAFE_SCRIPT_NAMES:
        return True
    return bool(re.search(r"(?:^|/)(?:title|select|choice|staff|ending|omake|scenario)[^/]*\.ks$", rel))


def _is_kirikiri_runtime_overlay_patch_file(file_name: str) -> bool:
    rel = str(file_name or "").replace("\\", "/").lstrip("/").lower()
    if not rel or _is_kirikiri_system_or_logic_script(rel):
        return False

    suffix = Path(rel).suffix.lower()
    if suffix == ".scn":
        return True
    if suffix != ".ks":
        return False

    parts = rel.split("/")
    if parts[0] == "scenario":
        if len(parts) >= 2 and parts[1] in {
            "backlog",
            "commonclass",
            "config",
            "db",
            "dbug",
            "debug",
            "effect",
            "macro",
            "plugin",
            "system",
            "ui",
        }:
            return False
        leaf = parts[-1]
        if leaf.startswith("_") or leaf in {"_first.ks", "scr.ks"}:
            return False
        if re.search(r"(?:^|[_-])(?:adv_)?start(?:[_-]|\.)", leaf):
            return False
        if len(parts) >= 2 and parts[1] in {"main", "route", "routes", "chapter", "chapters"}:
            return True
        if re.fullmatch(r"\d{1,3}(?:[_-].+)?\.ks", leaf):
            return True
        if re.match(r"^[0-9a-z]+[_-]", leaf):
            return True
        return bool(re.search(r"(?:adv|scene|story|scenario|event|episode|chapter|route|common|prologue|epilogue)", rel))

    return _is_kirikiri_flat_scenario_script(rel) or _is_kirikiri_safe_default_repack_file(rel)


def _is_kirikiri_safe_repack_item(item: TextItem) -> bool:
    rel = str(getattr(item, "file", "") or "").replace("\\", "/").lstrip("/")
    lowered = rel.lower()
    if not lowered:
        return False
    leaf = Path(lowered).name
    if _is_kirikiri_system_or_logic_script(lowered):
        return False
    if lowered.endswith(".tjs"):
        return False

    meta = getattr(item, "meta", {}) or {}
    suffix = Path(lowered).suffix
    xp3_filter = meta.get("xp3_filter")
    archive = str(xp3_filter.get("archive") or "") if isinstance(xp3_filter, dict) else ""
    from_engine_resource = (
        bool(meta.get("from_xp3"))
        or bool(meta.get("runtime_dump"))
        or bool(meta.get("runtime_capture"))
        or isinstance(meta.get("xp3_filter"), dict)
        or str(meta.get("format") or "") == "kirikiri_psb_scn"
    )
    if from_engine_resource and suffix == ".scn":
        return True
    if from_engine_resource and suffix == ".ks":
        return _is_kirikiri_runtime_overlay_patch_file(lowered) or (
            _is_kirikiri_scenario_archive_name(archive)
            and _is_kirikiri_flat_scenario_script(lowered)
        ) or (
            "/" not in lowered
            and isinstance(xp3_filter, dict)
            and not leaf.startswith("_")
            and leaf not in {"startup.ks", "macro.ks", "config.ks"}
        )
    if from_engine_resource and suffix == "" and bool(meta.get("from_xp3")):
        return True
    return _is_kirikiri_safe_default_repack_file(rel)


def _looks_like_kirikiri_control_identifier(text: str) -> bool:
    if not text:
        return False
    if re.fullmatch(r"\*[A-Za-z0-9_./\\:-]+", text):
        return True
    if re.fullmatch(r"[A-Za-z0-9_./\\:-]+\.(?:ks|tjs|scn|xp3|png|jpg|jpeg|webp|ogg|wav|mp3|m4a|mp4|avi)", text, re.I):
        return True
    return False


def _extract_kirikiri_text_spans(
    line: str,
    *,
    script_suffix: str = "",
    allow_plain_kag: bool = True,
) -> list[_TextSpan]:
    stripped = line.strip()
    if not stripped or stripped.startswith(";") or stripped.startswith("//") or stripped.startswith("*"):
        return []
    if len(stripped) > 1200:
        return []

    if script_suffix == ".tjs":
        return _extract_tjs_text_spans(stripped, line_offset=len(line) - len(line.lstrip()))

    if stripped.startswith("@") or stripped.startswith("["):
        inline_span = _extract_kag_bracket_message_span(line)
        if inline_span is not None:
            return [inline_span]
        results = _extract_command_texts(stripped)
        visible_tail = _extract_kag_visible_tail(stripped) if allow_plain_kag else ""
        if visible_tail and _looks_like_plain_kag_text(visible_tail):
            start = line.rfind(visible_tail)
            if start >= 0:
                results.append(_TextSpan(visible_tail, start, start + len(visible_tail)))
        return results
    if _is_tjs_code_line(stripped):
        return _extract_quoted_texts(stripped, line_offset=len(line) - len(line.lstrip()))
    if allow_plain_kag and _looks_like_inline_kag_text(stripped):
        start = line.find(stripped)
        return [_TextSpan(stripped, start, start + len(stripped))]
    if allow_plain_kag and _looks_like_plain_kag_text(stripped):
        start = line.find(stripped)
        return [_TextSpan(stripped, start, start + len(stripped))]
    return _extract_quoted_texts(stripped, line_offset=len(line) - len(line.lstrip()))


def _extract_tjs_text_spans(line: str, *, line_offset: int = 0) -> list[_TextSpan]:
    if not ('"' in line or "'" in line):
        return []
    if "禁則文字" in line or "execStorage" in line or "Plugins.link" in line:
        return []
    if re.match(r"^\s*var\s+(?:title|caption|label|message|text|name)\b", line, re.I):
        return _extract_quoted_texts(line, tjs=True, line_offset=line_offset)
    if re.search(r"\b(?:storage|file|path|folder|directory|fontFace)\s*[:=]", line, re.I):
        return []
    if not _TJS_TEXT_CONTEXT_RE.search(line) and not re.search(r"[。！？!?]", line):
        return []
    return _extract_quoted_texts(line, tjs=True, line_offset=line_offset)


def _extract_kag_visible_tail(line: str) -> str:
    if not line.startswith("["):
        return ""
    cursor = 0
    while cursor < len(line) and line[cursor] == "[":
        end = line.find("]", cursor + 1)
        if end < 0 or end - cursor > 120:
            return ""
        tag_body = line[cursor + 1:end]
        if "=" in tag_body or re.search(r"\s", tag_body):
            return ""
        cursor = end + 1
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
    return line[cursor:].strip()


def _extract_kag_bracket_message_span(line: str) -> _TextSpan | None:
    stripped = line.strip()
    first_tag = re.match(r"^\[([^\]]+)\]", stripped)
    if first_tag and ("=" in first_tag.group(1) or re.search(r"\s", first_tag.group(1))):
        return None
    match = _KAG_BRACKET_MESSAGE_RE.match(stripped)
    if not match:
        return None
    text = match.group(2).strip()
    if not _looks_like_plain_kag_text(text):
        return None
    stripped_offset = len(line) - len(line.lstrip())
    start = stripped_offset + match.start(2)
    while start < len(line) and line[start].isspace():
        start += 1
    end = start + len(text)
    return _TextSpan(text, start, end)


def _extract_command_texts(line: str) -> list[_TextSpan]:
    results = []
    command = _kag_command_name(line)
    if command not in _KAG_SAFE_TEXT_ATTR_COMMANDS:
        return results
    for match in _KAG_COMMAND_TEXT_ATTR_RE.finditer(line):
        text = _unescape_script_string(match.group(1))
        if _looks_like_game_text(text):
            results.append(_TextSpan(text, match.start(1), match.end(1)))
    return results


def _is_unsafe_kag_command_attr_span(line: str, start: int, end: int) -> bool:
    stripped = line.strip()
    if not (stripped.startswith("@") or stripped.startswith("[")):
        return False
    command = _kag_command_name(stripped)
    if command in _KAG_SAFE_TEXT_ATTR_COMMANDS:
        return False
    offset = len(line) - len(line.lstrip())
    local_start = start - offset
    local_end = end - offset
    for match in _KAG_COMMAND_TEXT_ATTR_RE.finditer(stripped):
        if match.start(1) <= local_start and local_end <= match.end(1):
            return True
    return False


def _is_unsafe_kag_command_attr_item(line: str, item: TextItem) -> bool:
    original = str(getattr(item, "original", "") or "")
    if not original:
        return False
    stripped = line.strip()
    if not (stripped.startswith("@") or stripped.startswith("[")):
        return False
    command = _kag_command_name(stripped)
    if command in _KAG_SAFE_TEXT_ATTR_COMMANDS:
        return False
    for match in _KAG_COMMAND_TEXT_ATTR_RE.finditer(stripped):
        if match.group(1) == original:
            return True
    return False


def _kag_command_name(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("@"):
        match = re.match(r"^@([A-Za-z_][\w.-]*)", stripped)
        return match.group(1).lower() if match else ""
    if stripped.startswith("["):
        match = re.match(r"^\[([^\s\]]+)", stripped)
        return match.group(1).lower() if match else ""
    return ""


def _extract_quoted_texts(line: str, *, tjs: bool = False, line_offset: int = 0) -> list[_TextSpan]:
    results: list[_TextSpan] = []
    for match in _QUOTED_STRING_RE.finditer(line):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        text = _unescape_script_string(raw)
        if (tjs and _looks_like_tjs_game_text(text)) or (not tjs and _looks_like_game_text(text)):
            start = match.start(1) if match.group(1) is not None else match.start(2)
            end = match.end(1) if match.group(1) is not None else match.end(2)
            results.append(_TextSpan(text, line_offset + start, line_offset + end))
    return results


def _is_tjs_code_line(stripped: str) -> bool:
    return bool(
        _TJS_CODE_STARTS.match(stripped)
        or _TJS_ASSIGNMENT.match(stripped)
        or stripped in {"{", "}", "};", ");"}
        or _TJS_LINE_COMMENT.match(stripped)
    )


def _looks_like_plain_kag_text(text: str) -> bool:
    if "=" in text and re.search(r"\b[A-Za-z_][\w.-]*\s*=", text):
        return False
    if text.startswith("#"):
        return False
    if "[" in text and "]" in text:
        return False
    if not _has_kirikiri_visible_text_chars(text):
        return False
    return _looks_like_game_text(text)


def _looks_like_inline_kag_text(text: str) -> bool:
    if "[" not in text or "]" not in text:
        return False
    if re.match(r"^\s*\[[A-Za-z_][^\]]*\]\s*$", text):
        return False
    visible = _strip_inline_kag_tags(text)
    return bool(visible and _has_kirikiri_visible_text_chars(visible) and _looks_like_game_text(visible))


def _strip_inline_kag_tags(text: str) -> str:
    text = re.sub(r"\[(?:ruby|ch)\s+[^\]]*\]", "", text, flags=re.I)
    text = re.sub(r"\[(?:emb|font|resetfont|r|l|p|wq|wait|nowait|endlink|link)[^\]]*\]", "", text, flags=re.I)
    text = re.sub(r"\[[A-Za-z_][^\]]*\]", "", text)
    return text.strip()


def _has_kirikiri_visible_text_chars(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\uff66-\uff9f\u3400-\u9fff]", str(text or "")))


def _looks_like_game_text(text: str) -> bool:
    text = text.strip()
    if len(text) <= 1 or len(text) >= 600:
        return False
    if re.fullmatch(r"\*[A-Za-z0-9_./\\:-]+", text):
        return False
    if re.fullmatch(r"[A-Za-z0-9_./\\:-]+", text):
        return False
    return True


def _looks_like_tjs_game_text(text: str) -> bool:
    stripped = text.strip()
    if not _looks_like_game_text(stripped):
        return False
    if stripped.lower() in _COMMON_FONT_NAMES:
        return False
    if not re.search(r"[\u3040-\u30ff\uff66-\uff9f\u3400-\u9fff]", stripped):
        return False
    if re.fullmatch(r"[\s%(),:;!?.\[\]{}'\"。、。，．：；・゛゜ヽヾゝゞ々’”）〕］｝〉》」』】°′″℃¢％‰]+", stripped):
        return False
    return True


def _is_runtime_capture_text(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > 1200:
        return False
    if any(0 < ord(ch) < 32 and ch not in "\n\r\t" for ch in stripped):
        return False
    if re.fullmatch(r"[\d\s.,:;!?()[\]{}'\"`~+\-*/\\|_=<>#$%&^@]+", stripped):
        return False
    if re.fullmatch(r"[A-Za-z0-9_./\\:-]+", stripped):
        return False
    if re.search(r"[\u3040-\u30ff\uff66-\uff9f]", stripped):
        return True
    return is_translatable(stripped)


def _clean_runtime_capture_text(text: str) -> str:
    stripped = (text or "").replace("\u0000", "").strip()
    if not stripped:
        return ""
    out: list[str] = []
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        j = i + 1
        while j < len(stripped) and stripped[j] == ch:
            j += 1
        run = j - i
        if run >= 3 and re.match(r"[\u3040-\u30ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff]", ch):
            out.append(ch)
        elif run >= 3 and re.match(r"[0-9０-９]", ch):
            out.append(ch * 2)
        else:
            out.append(stripped[i:j])
        i = j
    cleaned = "".join(out)
    cleaned = _collapse_common_measure_overlap(cleaned)
    cleaned = re.sub(r"([「『])\s+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([」』、。，．！？!?])", r"\1", cleaned)
    return cleaned.strip()


def _collapse_common_measure_overlap(text: str) -> str:
    text = re.sub(r"([\u3040-\u30ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff])\1(?=[「『])", r"\1", text)
    text = re.sub(r"([はをがにでとへも])\1(?=[\u3040-\u30ff\uff66-\uff9f\u3400-\u4dbf\u4e00-\u9fff])", r"\1", text)
    return text


def _unescape_script_string(text: str) -> str:
    return (
        text.replace(r"\"", '"')
        .replace(r"\'", "'")
        .replace(r"\n", "\n")
        .replace(r"\t", "\t")
        .replace(r"\\", "\\")
    )
