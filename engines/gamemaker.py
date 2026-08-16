from __future__ import annotations

import re
import shutil
import struct
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, warning
from utils.text_extract import is_translatable


class GameMakerEngine(EngineBase):
    name = "gamemaker"
    label = "GameMaker / YoYo 引擎"
    support_level = "partial"
    supports_extract = True
    supports_repack = True
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=True,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "从 data.win STRG 和主程序数据段提取文本。",
            "静态回填仅写入能放回原槽位的译文，运行时覆盖用于补充显示层文本。",
        ),
    )
    detect_priority = 88
    limitations = [
        "可从 data.win 的 STRG 字符串表以及主程序数据段提取候选文本。",
        "可安全回填主程序中 UTF-8 字节长度不超过原文槽位的字符串，超长译文会跳过。",
        "暂不自动回填 data.win：全量重建资源包需要更新跨 chunk 偏移，直接写回风险较高。",
    ]

    def detect(self, path: Path) -> bool:
        data_win = self._find_data_win(path)
        return bool(data_win and _is_gamemaker_data_win(data_win))

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        data_win = self._find_data_win(path)
        if not data_win or not _is_gamemaker_data_win(data_win):
            return 0, []
        return 90, [f"找到 GameMaker data.win: {data_win.name}", "FORM/GEN8/STRG chunk 匹配"]

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        data_win = self._find_data_win(game_dir)
        if not data_win:
            warning("未找到 data.win")
            return []

        original_dir = workspace / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(data_win, original_dir / "data.win.source")
        except Exception:
            pass

        entries = _read_strg_entries(data_win)
        items: list[TextItem] = []
        seen: set[str] = set()
        for index, offset, byte_len, text in entries:
            clean = _normalize_text(text)
            if not clean or clean in seen or not _looks_like_game_text(clean):
                continue
            seen.add(clean)
            items.append(TextItem(
                file="data.win",
                key=f"STRG[{index}]@0x{offset:X}",
                original=clean,
                context="GameMaker STRG string table",
                meta={
                    "index": index,
                    "offset": offset,
                    "byte_len": byte_len,
                    "source": str(data_win),
                    "engine_note": "extract_only_no_safe_repack",
                },
            ))

        exe_items = self._extract_executable_strings(path, seen)
        items.extend(exe_items)

        info(
            f"GameMaker STRG: {len(entries)} 条字符串，筛出 {len(items) - len(exe_items)} 条；"
            f"主程序数据段筛出 {len(exe_items)} 条可翻译文本"
        )
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        stats = _patch_executable_strings(items)
        if stats["patched"]:
            info(
                "GameMaker 主程序安全回填: "
                f"{stats['patched']} 条已写入，{stats['skipped_too_long']} 条超长跳过，"
                f"{stats['skipped_unsupported']} 条非 exe 条目跳过"
            )
        else:
            warning(
                "GameMaker 未写入任何主程序文本："
                f"{stats['skipped_too_long']} 条超长，{stats['skipped_unsupported']} 条不支持"
            )
        if stats["skipped_data_win"]:
            warning(f"GameMaker data.win 回填暂未启用，跳过 {stats['skipped_data_win']} 条。")

        font_stats = _patch_gamemaker_font_for_items(items, getattr(self, "_game_dir", None))
        if font_stats["patched_fonts"]:
            info(
                "GameMaker CJK 字体补丁: "
                f"{font_stats['patched_fonts']} 个字体，"
                f"{font_stats['patched_glyphs']} 个字形槽，"
                f"{font_stats['rendered_glyphs']} 个新渲染字形"
            )
        elif font_stats["needed_chars"] and font_stats["skipped"]:
            warning(
                "GameMaker CJK 字体补丁跳过: "
                f"{font_stats['skipped']} 个问题，{font_stats['missing_glyphs']} 个缺失字形"
            )

    def find_exe(self, path: Path) -> Path | None:
        from core.exe_selector import find_main_exe

        if path.is_file() and path.suffix.lower() == ".exe":
            return path
        return find_main_exe(path, recursive=False) or super().find_exe(path)

    def _extract_executable_strings(self, game_dir: Path, seen: set[str]) -> list[TextItem]:
        exe = self.find_exe(game_dir)
        if not exe:
            return []

        items: list[TextItem] = []
        for section, offset, text in _read_pe_data_strings(exe):
            clean = _normalize_text(text)
            if not clean or not _looks_like_executable_game_text(clean):
                continue
            if clean in seen and not _is_known_ui_text(clean):
                continue
            seen.add(clean)
            items.append(TextItem(
                file=exe.name,
                key=f"PE[{section}]@0x{offset:X}",
                original=clean,
                context="GameMaker executable data string",
                meta={
                    "offset": offset,
                    "byte_len": len(clean.encode("utf-8", errors="strict")),
                    "section": section,
                    "source": str(exe),
                    "source_kind": "executable_string",
                    "engine_note": "safe_exe_repack_if_translation_fits_original_utf8_bytes",
                },
            ))
        return items

    @staticmethod
    def _find_data_win(path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        direct = game_dir / "data.win"
        if direct.is_file():
            return direct
        for sub in ("game", "data", "assets"):
            candidate = game_dir / sub / "data.win"
            if candidate.is_file():
                return candidate
        return None


def _is_gamemaker_data_win(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            if f.read(4) != b"FORM":
                return False
            f.read(4)
            return f.read(4) in {b"GEN8", b"GMX "}
    except Exception:
        return False


def _read_chunks(path: Path) -> dict[str, tuple[int, int]]:
    chunks: dict[str, tuple[int, int]] = {}
    size = path.stat().st_size
    with path.open("rb") as f:
        if f.read(4) != b"FORM":
            return chunks
        f.read(4)
        pos = 8
        while pos + 8 <= size:
            f.seek(pos)
            name_raw = f.read(4)
            if len(name_raw) < 4:
                break
            name = name_raw.decode("ascii", errors="replace")
            chunk_size = int.from_bytes(f.read(4), "little")
            chunks[name] = (pos + 8, chunk_size)
            pos += 8 + chunk_size
    return chunks


def _read_strg_entries(path: Path) -> list[tuple[int, int, int, str]]:
    chunks = _read_chunks(path)
    if "STRG" not in chunks:
        return []
    start, size = chunks["STRG"]
    end = start + size
    entries: list[tuple[int, int, int, str]] = []
    with path.open("rb") as f:
        f.seek(start)
        count = int.from_bytes(f.read(4), "little")
        if count <= 0 or count > 2_000_000:
            return []
        offsets = [int.from_bytes(f.read(4), "little") for _ in range(count)]
        for index, offset in enumerate(offsets):
            if offset < start or offset + 4 > end:
                continue
            f.seek(offset)
            byte_len = int.from_bytes(f.read(4), "little")
            if byte_len <= 0 or byte_len > 64 * 1024 or offset + 4 + byte_len > end:
                continue
            raw = f.read(byte_len)
            nul = f.read(1)
            if nul not in (b"", b"\0"):
                continue
            text = _decode_string(raw)
            if text is not None:
                entries.append((index, offset, byte_len, text))
    return entries


def _decode_string(raw: bytes) -> str | None:
    for enc in ("utf-8", "cp1252", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _read_pe_data_strings(path: Path) -> list[tuple[str, int, str]]:
    """Read null-terminated strings from PE data sections.

    YYC GameMaker games may compile game-facing string literals into the main
    executable instead of keeping every literal in data.win's STRG table. We only
    scan writable non-code sections to avoid pulling thousands of runner/runtime
    messages from .text/.rdata.
    """
    try:
        data = path.read_bytes()
    except Exception:
        return []

    sections = _pe_data_sections(data)
    items: list[tuple[str, int, str]] = []
    for name, raw_ptr, raw_size in sections:
        if raw_ptr < 0 or raw_size <= 0 or raw_ptr >= len(data):
            continue
        chunk = data[raw_ptr: min(len(data), raw_ptr + raw_size)]
        for offset, raw in _iter_c_strings(chunk, base_offset=raw_ptr):
            text = _decode_string(raw)
            if text is not None:
                items.append((name, offset, text))
    return items


def _patch_executable_strings(items: list[TextItem]) -> dict[str, int]:
    stats = {
        "patched": 0,
        "skipped_too_long": 0,
        "skipped_unsupported": 0,
        "skipped_data_win": 0,
        "missing_source": 0,
        "mismatch": 0,
    }
    by_source: dict[Path, list[TextItem]] = {}

    for item in items:
        if not item.translated or item.translated == item.original:
            continue
        if item.file == "data.win":
            stats["skipped_data_win"] += 1
            item.translated = item.original
            item.meta["repack_status"] = "skipped_data_win_unsupported"
            continue
        if item.meta.get("source_kind") != "executable_string":
            stats["skipped_unsupported"] += 1
            item.translated = item.original
            item.meta["repack_status"] = "skipped_unsupported_source_kind"
            continue
        source = item.meta.get("source")
        if not source:
            stats["missing_source"] += 1
            item.translated = item.original
            item.meta["repack_status"] = "missing_source"
            continue
        by_source.setdefault(Path(source), []).append(item)

    for source, source_items in by_source.items():
        if not source.is_file():
            stats["missing_source"] += len(source_items)
            for item in source_items:
                item.translated = item.original
                item.meta["repack_status"] = "missing_source"
            continue
        data = bytearray(source.read_bytes())
        changed = False
        for item in source_items:
            offset = item.meta.get("offset")
            byte_len = item.meta.get("byte_len")
            if not isinstance(offset, int) or not isinstance(byte_len, int) or byte_len <= 0:
                stats["skipped_unsupported"] += 1
                item.meta["repack_status"] = "missing_offset_or_length"
                continue

            original_raw = item.original.encode("utf-8", errors="strict")
            fitted = _fit_executable_translation(item.original, item.translated, byte_len)
            if fitted != item.translated:
                item.meta["repack_note"] = "translation_shortened_to_fit_fixed_slot"
                item.meta["requested_translation"] = item.translated
                item.translated = fitted
            translated_raw = item.translated.encode("utf-8", errors="strict")
            if len(translated_raw) > byte_len:
                stats["skipped_too_long"] += 1
                item.translated = item.original
                item.meta["repack_status"] = "skipped_too_long"
                item.meta["translated_byte_len"] = len(translated_raw)
                continue
            if offset < 0 or offset + byte_len > len(data):
                stats["mismatch"] += 1
                item.translated = item.original
                item.meta["repack_status"] = "offset_out_of_range"
                continue
            if bytes(data[offset:offset + len(original_raw)]) != original_raw:
                stats["mismatch"] += 1
                item.translated = item.original
                item.meta["repack_status"] = "original_mismatch"
                continue

            data[offset:offset + byte_len] = translated_raw + b"\0" * (byte_len - len(translated_raw))
            stats["patched"] += 1
            changed = True
            item.meta["repack_status"] = "patched_exe_in_place"
            item.meta["translated_byte_len"] = len(translated_raw)

        if changed:
            source.write_bytes(data)
    return stats


def _fit_executable_translation(original: str, translated: str, byte_len: int) -> str:
    """Return a translation that fits a fixed UTF-8 byte slot when possible."""
    if len(translated.encode("utf-8", errors="strict")) <= byte_len:
        return translated

    for candidate in _short_translation_candidates(original, translated):
        if len(candidate.encode("utf-8", errors="strict")) <= byte_len:
            return candidate
    return translated


def _short_translation_candidates(original: str, translated: str) -> list[str]:
    candidates: list[str] = []

    def add(value: str | None):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    normalized_original = _normalize_ui_key(original)
    add(_EXE_SHORT_TRANSLATIONS.get(normalized_original))

    # Drop common punctuation the translator may add to compact menu labels.
    compact = translated.strip().strip(" .。:：;；!！?？")
    add(compact)
    add(compact.replace(" ", ""))

    if normalized_original.startswith("aim ") and len(original.split()) == 2:
        axis = original.split()[-1].upper()
        add(f"瞄{axis}")
    if normalized_original.endswith(" menu"):
        direction = normalized_original[:-5]
        add({
            "left": "左项",
            "right": "右项",
            "up": "上项",
            "down": "下项",
        }.get(direction))
    if normalized_original.endswith(" mode"):
        base = normalized_original[:-5]
        add(_EXE_SHORT_TRANSLATIONS.get(base))

    return candidates


def _normalize_ui_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).lower()


def _pe_data_sections(data: bytes) -> list[tuple[str, int, int]]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return []
    try:
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        if pe_offset <= 0 or pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
            return []
        section_count = struct.unpack_from("<H", data, pe_offset + 6)[0]
        opt_header_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
        section_table = pe_offset + 24 + opt_header_size
    except struct.error:
        return []

    sections: list[tuple[str, int, int]] = []
    for index in range(section_count):
        offset = section_table + index * 40
        if offset + 40 > len(data):
            break
        name_raw = data[offset:offset + 8].split(b"\0", 1)[0]
        name = name_raw.decode("ascii", errors="replace")
        try:
            raw_size = struct.unpack_from("<I", data, offset + 16)[0]
            raw_ptr = struct.unpack_from("<I", data, offset + 20)[0]
            characteristics = struct.unpack_from("<I", data, offset + 36)[0]
        except struct.error:
            continue

        is_code = bool(characteristics & 0x00000020)
        is_writable = bool(characteristics & 0x80000000)
        if is_writable and not is_code:
            sections.append((name, raw_ptr, raw_size))
    return sections


def _iter_c_strings(data: bytes, base_offset: int = 0,
                    min_len: int = 2, max_len: int = 160):
    start: int | None = None
    buf = bytearray()
    for idx, byte in enumerate(data):
        if byte in (9, 10, 13) or 32 <= byte <= 126:
            if start is None:
                start = idx
            buf.append(byte)
            if len(buf) > max_len:
                start = None
                buf.clear()
        else:
            if start is not None and len(buf) >= min_len:
                yield base_offset + start, bytes(buf)
            start = None
            buf.clear()
    if start is not None and len(buf) >= min_len:
        yield base_offset + start, bytes(buf)


_TECH_PREFIXES = (
    "@@", "gml_", "scr_", "obj_", "spr_", "snd_", "rm_", "bg_", "shd_",
    "steam_", "achievement_", "global.", "argument", "GMLive", "fcmd_",
)
_CODE_MARKERS = (
    "#line ", "\nvar ", "\nif ", "\nwhile ", "\nfor ", "function(",
    " = ", "==", "!=", ">=", "<=", "++", "--", "->",
)


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _looks_like_game_text(text: str) -> bool:
    if not (2 <= len(text) <= 500):
        return False
    if "\x00" in text:
        return False
    if text.startswith(_TECH_PREFIXES):
        return False
    if any(marker in text for marker in _CODE_MARKERS):
        return False
    if re.fullmatch(r"[A-Za-z0-9_.:/\\\-]+", text):
        return False
    if re.fullmatch(r"[A-Z0-9_]{3,}", text):
        return False
    if text.count("_") >= 2 and " " not in text:
        return False
    if sum(ch.isalpha() for ch in text) < 2:
        return False
    return is_translatable(text)


_EXE_BAD_SUBSTRINGS = tuple(s.lower() for s in (
    ".dll", ".exe", ".cpp", ".h", ".hpp", ".pdb", ".png", ".jpg", ".jpeg",
    ".ogg", ".wav", ".mp3", ".atlas", ".ini", "software\\", "program files",
    "windows\\", "steamapi", "steamclient", "directx", "d3d", "dxgi",
    "xinput", "exception", "error", "fatal", "memory allocation",
    "called from", "shader compilation", "collision event", "gamemaker",
    "runner", "zeus", "std::", "unordered_", "queryadapter", "colorprofile",
    "object_main", "object_class", "physics", "fixture", "vertex",
    "fragment", "http://", "https://",
    "debugged", "debugging", " debug", "didnt do", "didn't do",
    "updated ", " configs", " config", " sound emitter", " surf",
    " wrapper", "update ", "changelog",
))

_EXE_BAD_EXACT = {
    "A", "B", "X", "Y", "W", "S", "D", "R", "Q", "E",
    "L1", "L2", "L3", "R1", "R2", "R3", "LB", "LT", "RB", "RT",
    "PS", "MUI", "IME", "GUI", "EVNT", "Esc", "Tab", "Tilde", "Dig1",
    "Space", "Shift", "Enter", "Done!",
}

_EXE_SHORT_UI = {
    "OK", "Ok", "Yes", "No", "On", "Off", "Save", "Load", "Quit",
    "Cancel", "Continue", "Options", "Settings", "Language", "Back",
    "Apply", "Start", "Pause", "Select", "Use", "Shoot", "Dodge",
    "Sprint", "Ability", "Melee", "Reload", "Map", "Inventory",
    "Keyboard", "Mouse", "Controller", "Previous", "Next", "Weapon",
    "Chat", "Status", "Left", "Right", "Up", "Down", "Menu",
}

_EXE_SHORT_TRANSLATIONS = {
    "ok": "好",
    "yes": "是",
    "no": "否",
    "on": "开",
    "off": "关",
    "save": "存",
    "load": "读",
    "quit": "退",
    "play": "玩",
    "cancel": "取消",
    "continue": "继续",
    "options": "选项",
    "settings": "设置",
    "patch notes": "补丁",
    "credits": "制作",
    "language": "语言",
    "back": "返回",
    "apply": "应用",
    "start": "开始",
    "pause": "暂停",
    "select": "选择",
    "use": "用",
    "shoot": "射",
    "dodge": "闪",
    "sprint": "冲刺",
    "ability": "技能",
    "melee": "近战",
    "reload": "换弹",
    "map": "图",
    "inventory": "背包",
    "keyboard": "键盘",
    "mouse": "鼠标",
    "controller": "手柄",
    "previous": "上个",
    "next": "下个",
    "weapon": "武器",
    "chat": "聊天",
    "status": "状态",
    "left": "左",
    "right": "右",
    "up": "上",
    "down": "下",
    "menu": "菜单",
    "dev menu": "开发",
    "world select": "选世界",
    "left menu": "左栏",
    "right menu": "右栏",
    "up menu": "上栏",
    "down menu": "下栏",
    "play normal": "开始",
    "reset settings": "重置",
    "multiplayer": "多人",
    "delete save": "删档",
    "early access": "抢先",
    "roadmap": "路线图",
    "play time": "时间",
    "runs": "局数",
    "deaths": "死亡",
    "wins": "胜利",
    "next weapon": "下把武器",
    "previous weapon": "上把武器",
    "chat status": "聊天状态",
    "aim x": "瞄X",
    "aim y": "瞄Y",
    "god": "神",
    "ghost": "幽灵",
    "lock": "锁",
}


_CJK_FONT_PRIORITY = (
    "玩设置补丁制作退开始继续选项返回确认取消应用保存读取语言"
    "上下左右菜单武器射击闪避冲刺技能近战换弹地图背包键盘鼠标手柄"
    "是是否开关暂停选择使用聊天状态难度生命伤害速度时间数量"
)
_CJK_MAX_GLYPHS = 32
_CJK_PREFERRED_CELLS = ((16, 16), (15, 15), (14, 14))


def _is_known_ui_text(text: str) -> bool:
    return text in _EXE_SHORT_UI or _normalize_ui_key(text) in _EXE_SHORT_TRANSLATIONS


def _looks_like_executable_game_text(text: str) -> bool:
    if not (2 <= len(text) <= 160):
        return False
    if "\x00" in text:
        return False
    if text.startswith(_TECH_PREFIXES):
        return False
    is_known_ui = _is_known_ui_text(text)
    if not is_translatable(text) and not is_known_ui:
        return False
    low = text.lower()
    if any(part in low for part in _EXE_BAD_SUBSTRINGS):
        return False
    if text in _EXE_BAD_EXACT:
        return False
    if any(marker in text for marker in _CODE_MARKERS):
        return False
    if re.search(r"[^\w\s.,:;!?()'’+\-/]", text):
        return False
    if len(text) <= 5 and not is_known_ui:
        if not re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+)?", text):
            return False
        if text.endswith("?"):
            return False
    if re.search(r"[`{}\[\]]", text):
        return False
    if re.search(r"%[0-9.]*[a-zA-Z]", text):
        return False
    if re.search(r"[\\/|]", text):
        return False
    if text.count("_") >= 1:
        return False
    if re.fullmatch(r"[A-Za-z]{1,3}[A-Z][A-Za-z]?", text) and not is_known_ui:
        return False
    if re.fullmatch(r"[A-Z0-9_ ()/-]{4,}", text) and not is_known_ui:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.:\-]+", text) and " " not in text and not is_known_ui:
        return False
    if len(text) <= 3 and not is_known_ui:
        return False
    letters = sum(ch.isalpha() for ch in text)
    if letters / max(len(text), 1) < 0.45:
        return False

    words = re.findall(r"[A-Za-z]+", text)
    if len(words) >= 2 and text[0].islower():
        return False
    if len(words) >= 2 and all(word.islower() for word in words) and not re.search(r"[.!?]", text):
        return False
    if re.fullmatch(r"[A-Za-z]+", text):
        if text[0].islower():
            return False
        if not is_known_ui and len(text) > 10:
            return False
    return True


@dataclass
class _FontRecord:
    start: int
    glyph_count: int
    glyph_pointer_base: int
    glyph_ptrs: list[int]
    page_ptr: int
    page: tuple[int, int, int, int, int, int, int, int, int, int, int]


def _patch_gamemaker_font_for_items(items: list[TextItem], game_dir_obj) -> dict[str, int]:
    stats = {
        "needed_chars": 0,
        "patched_fonts": 0,
        "patched_glyphs": 0,
        "rendered_glyphs": 0,
        "missing_glyphs": 0,
        "skipped": 0,
    }
    needed = _collect_cjk_chars(item.translated for item in items if item.translated and item.translated != item.original)
    stats["needed_chars"] = len(needed)
    if not needed:
        return stats

    game_dir = Path(game_dir_obj) if game_dir_obj else None
    if not game_dir:
        source = next((item.meta.get("source") for item in items if item.meta.get("source")), "")
        if source:
            game_dir = Path(source).parent
    if not game_dir:
        stats["skipped"] += 1
        return stats

    data_win = GameMakerEngine._find_data_win(game_dir)
    if not data_win or not data_win.is_file():
        stats["skipped"] += 1
        return stats

    patch = _build_gamemaker_font_patch(data_win, needed)
    for key, value in patch.items():
        stats[key] = stats.get(key, 0) + value
    return stats


def _collect_cjk_chars(values) -> list[str]:
    counts: Counter[str] = Counter()
    for value in values:
        for ch in value:
            code = ord(ch)
            if not _is_cjk_codepoint(code):
                continue
            counts[ch] += 1
    priority_text = (
        "玩设置补丁制作退出开始继续选项返回确认取消应用保存读取语言"
        "上下左右菜单武器射击闪避冲刺技能近战换弹地图背包键盘鼠标手柄使用"
        "聊天状态难度生命伤害数量开关暂停选择是否"
    ) + _CJK_FONT_PRIORITY
    priority = {ch: index for index, ch in enumerate(priority_text)}
    return [
        ch for ch, _count in sorted(
            counts.items(),
            key=lambda item: (priority.get(item[0], len(priority)), -item[1], ord(item[0])),
        )
    ]


def _is_cjk_codepoint(code: int) -> bool:
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
    )


def _build_gamemaker_font_patch(data_win: Path, chars: list[str]) -> dict[str, int]:
    stats = {
        "patched_fonts": 0,
        "patched_glyphs": 0,
        "rendered_glyphs": 0,
        "missing_glyphs": 0,
        "skipped": 0,
    }
    try:
        data = bytearray(data_win.read_bytes())
    except Exception as exc:
        warning(f"GameMaker CJK font patch: cannot read data.win: {exc}")
        stats["skipped"] += 1
        return stats

    chunks = _read_chunks_from_bytes(data)
    if not all(name in chunks for name in ("FONT", "TPAG", "TXTR")):
        stats["skipped"] += 1
        return stats

    fonts = _read_gamemaker_fonts(data, chunks)
    if not fonts:
        stats["skipped"] += 1
        return stats

    changed = False
    for font in fonts:
        patchable: list[tuple[str, int | None]] = []
        for ch in chars:
            existing = _find_font_glyph_ptr(data, font, ord(ch))
            if existing is None:
                patchable.append((ch, None))
                continue
            _code, _x, _y, w, h, _shift, _offset, _tpage = struct.unpack_from("<HHHHHHHH", data, existing)
            if w < 14 or h < 14 or _x + w > font.page[2] or _y + h > font.page[3]:
                patchable.append((ch, existing))

        if not patchable:
            continue

        slots: list[int] = []
        missing_chars = [ch for ch, ptr in patchable if ptr is None]
        if missing_chars:
            reserved_ptrs = {ptr for _ch, ptr in patchable if ptr is not None}
            slots = _select_reusable_glyph_slots(data, font, len(missing_chars), reserved_ptrs=reserved_ptrs)
            if not slots:
                stats["missing_glyphs"] += len(missing_chars)
                continue
            if len(slots) < len(missing_chars):
                stats["missing_glyphs"] += len(missing_chars) - len(slots)
                available_missing = set(missing_chars[:len(slots)])
                patchable = [
                    (ch, ptr) for ch, ptr in patchable
                    if ptr is not None or ch in available_missing
                ]

        glyph_ptrs: list[int] = []
        chars_to_patch: list[str] = []
        slot_iter = iter(slots)
        missing_slot_budget = len(slots)
        used_missing_slots = 0
        for ch, ptr in patchable:
            if ptr is not None:
                chars_to_patch.append(ch)
                glyph_ptrs.append(ptr)
                continue
            if used_missing_slots >= missing_slot_budget:
                continue
            chars_to_patch.append(ch)
            glyph_ptrs.append(next(slot_iter))
            used_missing_slots += 1

        if len(glyph_ptrs) < len(chars_to_patch):
            stats["missing_glyphs"] += len(chars_to_patch) - len(glyph_ptrs)
            chars_to_patch = chars_to_patch[:len(glyph_ptrs)]
            glyph_ptrs = glyph_ptrs[:len(chars_to_patch)]
        if len(chars_to_patch) > _CJK_MAX_GLYPHS:
            stats["missing_glyphs"] += len(chars_to_patch) - _CJK_MAX_GLYPHS
            chars_to_patch = chars_to_patch[:_CJK_MAX_GLYPHS]
            glyph_ptrs = glyph_ptrs[:_CJK_MAX_GLYPHS]
        if not chars_to_patch:
            continue

        rendered_pairs = _patch_font_page_png(data, chunks, font, chars_to_patch, glyph_ptrs)
        if not rendered_pairs:
            stats["missing_glyphs"] += len(chars_to_patch)
            stats["skipped"] += 1
            continue

        if len(rendered_pairs) < len(chars_to_patch):
            stats["missing_glyphs"] += len(chars_to_patch) - len(rendered_pairs)

        for ch, ptr in rendered_pairs:
            _rewrite_glyph_codepoint(data, ptr, ord(ch))
        _sort_font_glyph_pointers(data, font)
        stats["patched_fonts"] += 1
        stats["patched_glyphs"] += len(rendered_pairs)
        stats["rendered_glyphs"] += len(rendered_pairs)
        if len(chars) > len(rendered_pairs):
            stats["missing_glyphs"] += len(chars) - len(rendered_pairs)
        changed = True

    if changed:
        try:
            data_win.write_bytes(data)
        except Exception as exc:
            warning(f"GameMaker CJK font patch: cannot write data.win: {exc}")
            stats["skipped"] += 1
    return stats


def _read_chunks_from_bytes(data: bytes) -> dict[str, tuple[int, int]]:
    chunks: dict[str, tuple[int, int]] = {}
    if len(data) < 12 or data[:4] != b"FORM":
        return chunks
    pos = 8
    while pos + 8 <= len(data):
        name_raw = data[pos:pos + 4]
        try:
            name = name_raw.decode("ascii")
        except UnicodeDecodeError:
            break
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        if start + size > len(data):
            break
        chunks[name] = (start, size)
        pos = start + size
    return chunks


def _read_pointer_list(data: bytes, chunk_start: int, chunk_size: int, max_count: int = 2_000_000) -> list[int]:
    if chunk_size < 4:
        return []
    count = struct.unpack_from("<I", data, chunk_start)[0]
    if count <= 0 or count > max_count or chunk_start + 4 + count * 4 > chunk_start + chunk_size:
        return []
    ptrs: list[int] = []
    for index in range(count):
        ptr = struct.unpack_from("<I", data, chunk_start + 4 + index * 4)[0]
        if 0 <= ptr < len(data):
            ptrs.append(ptr)
    return ptrs


def _read_gamemaker_fonts(data: bytes, chunks: dict[str, tuple[int, int]]) -> list[_FontRecord]:
    font_start, font_size = chunks["FONT"]
    font_ptrs = _read_pointer_list(data, font_start, font_size)
    fonts: list[_FontRecord] = []
    for ptr in font_ptrs:
        if ptr + 0x30 > len(data):
            continue
        page_ptr = struct.unpack_from("<I", data, ptr + 0x1C)[0]
        glyph_count = struct.unpack_from("<I", data, ptr + 0x2C)[0]
        glyph_pointer_base = ptr + 0x30
        if glyph_count <= 0 or glyph_count > 100_000:
            continue
        if glyph_pointer_base + glyph_count * 4 > len(data):
            continue
        if page_ptr < 0 or page_ptr + 22 > len(data):
            continue
        glyph_ptrs = [struct.unpack_from("<I", data, glyph_pointer_base + i * 4)[0] for i in range(glyph_count)]
        if any(gptr < 0 or gptr + 16 > len(data) for gptr in glyph_ptrs):
            continue
        page_values = struct.unpack_from("<HHHHHHHHHHH", data, page_ptr)
        fonts.append(_FontRecord(
            start=ptr,
            glyph_count=glyph_count,
            glyph_pointer_base=glyph_pointer_base,
            glyph_ptrs=glyph_ptrs,
            page_ptr=page_ptr,
            page=page_values,
        ))
    return fonts


def _font_has_codepoint(data: bytes, font: _FontRecord, codepoint: int) -> bool:
    return _find_font_glyph_ptr(data, font, codepoint) is not None


def _find_font_glyph_ptr(data: bytes, font: _FontRecord, codepoint: int) -> int | None:
    if codepoint > 0xFFFF:
        return None
    for ptr in font.glyph_ptrs:
        if struct.unpack_from("<H", data, ptr)[0] == codepoint:
            return ptr
    return None


def _select_reusable_glyph_slots(data: bytes, font: _FontRecord, needed_count: int,
                                 reserved_ptrs: set[int] | None = None) -> list[int]:
    reserved_ptrs = reserved_ptrs or set()
    candidates: list[tuple[int, int]] = []
    for ptr in font.glyph_ptrs:
        if ptr in reserved_ptrs:
            continue
        code = struct.unpack_from("<H", data, ptr)[0]
        if 0x0400 <= code <= 0x052F:
            candidates.append((code, ptr))
    if len(candidates) < needed_count:
        existing_ptrs = {ptr for _code, ptr in candidates}
        for ptr in font.glyph_ptrs:
            if ptr in reserved_ptrs or ptr in existing_ptrs:
                continue
            code = struct.unpack_from("<H", data, ptr)[0]
            if code >= 0x0100:
                candidates.append((code, ptr))
            if len(candidates) >= needed_count:
                break
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [ptr for _code, ptr in candidates[:needed_count]]


def _rewrite_glyph_codepoint(data: bytearray, glyph_ptr: int, codepoint: int) -> None:
    if 0 <= codepoint <= 0xFFFF:
        struct.pack_into("<H", data, glyph_ptr, codepoint)


def _sort_font_glyph_pointers(data: bytearray, font: _FontRecord) -> None:
    ptrs = [struct.unpack_from("<I", data, font.glyph_pointer_base + i * 4)[0] for i in range(font.glyph_count)]
    ptrs.sort(key=lambda ptr: struct.unpack_from("<H", data, ptr)[0])
    for index, ptr in enumerate(ptrs):
        struct.pack_into("<I", data, font.glyph_pointer_base + index * 4, ptr)
    font.glyph_ptrs = ptrs


def _patch_font_page_png(data: bytearray, chunks: dict[str, tuple[int, int]], font: _FontRecord,
                         chars: list[str], glyph_ptrs: list[int]) -> list[tuple[str, int]]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        warning(f"GameMaker CJK font patch: Pillow unavailable: {exc}")
        return []

    txtr_index = font.page[10]
    png_offset = _txtr_png_offset(data, chunks, txtr_index)
    if png_offset is None:
        return []
    png_len = _png_length(data, png_offset)
    if png_len <= 0:
        return []

    try:
        image = Image.open(BytesIO(bytes(data[png_offset:png_offset + png_len]))).convert("RGBA")
    except Exception as exc:
        warning(f"GameMaker CJK font patch: cannot decode font atlas PNG: {exc}")
        return []

    font_path = _find_system_cjk_font()
    if not font_path:
        warning("GameMaker CJK font patch: no system CJK font found")
        return []

    page_x, page_y, page_w, page_h = font.page[0], font.page[1], font.page[2], font.page[3]
    occupied = _font_occupied_rects(data, font, skip_ptrs=set(glyph_ptrs))
    placements = _allocate_cjk_font_rects(occupied, len(chars), page_w, page_h)
    if not placements:
        warning("GameMaker CJK font patch: no free atlas space for larger CJK glyphs")
        return []

    draw = ImageDraw.Draw(image)
    rendered: list[tuple[str, int]] = []
    for ch, glyph_ptr, placement in zip(chars, glyph_ptrs, placements):
        code, x, y, w, h, shift, offset, tpage = struct.unpack_from("<HHHHHHHH", data, glyph_ptr)
        x, y, w, h = placement
        if w <= 0 or h <= 0:
            continue
        draw_x = page_x + x
        draw_y = page_y + y
        draw.rectangle((draw_x, draw_y, draw_x + w + 1, draw_y + h + 1), fill=(0, 0, 0, 0))
        inner_w = max(1, w - 2)
        inner_h = max(1, h - 2)
        pil_font = _load_fitting_font(font_path, ch, inner_w, inner_h)
        if not pil_font:
            continue
        bbox = pil_font.getbbox(ch)
        text_w = max(1, bbox[2] - bbox[0])
        text_h = max(1, bbox[3] - bbox[1])
        tx = draw_x + 1 + max(0, (inner_w - text_w) // 2) - bbox[0]
        ty = draw_y + 1 + max(0, (inner_h - text_h) // 2) - bbox[1]
        draw.text((tx, ty), ch, font=pil_font, fill=(255, 255, 255, 255))
        new_shift = min(0xFFFF, w + 1)
        struct.pack_into("<HHHHHHHH", data, glyph_ptr, code, x, y, w, h, new_shift, offset, tpage)
        rendered.append((ch, glyph_ptr))

    if not rendered:
        return []

    out = BytesIO()
    image.save(out, format="PNG", optimize=True)
    png = out.getvalue()
    if len(png) > png_len:
        out = BytesIO()
        image.save(out, format="PNG", compress_level=9)
        png = out.getvalue()
    if len(png) > png_len:
        warning(
            "GameMaker CJK font patch: patched font atlas PNG is larger than the original slot "
            f"({len(png)} > {png_len})"
        )
        return []

    data[png_offset:png_offset + len(png)] = png
    data[png_offset + len(png):png_offset + png_len] = b"\0" * (png_len - len(png))
    return rendered


def _txtr_png_offset(data: bytes, chunks: dict[str, tuple[int, int]], texture_index: int) -> int | None:
    txtr_start, txtr_size = chunks["TXTR"]
    txtr_ptrs = _read_pointer_list(data, txtr_start, txtr_size)
    if texture_index < 0 or texture_index >= len(txtr_ptrs):
        return None
    record = txtr_ptrs[texture_index]
    if record + 12 > len(data):
        return None
    png_offset = struct.unpack_from("<I", data, record + 8)[0]
    if png_offset < 0 or png_offset + 8 > len(data):
        return None
    if data[png_offset:png_offset + 8] != b"\x89PNG\r\n\x1a\n":
        return None
    return png_offset


def _font_occupied_rects(data: bytes, font: _FontRecord, skip_ptrs: set[int] | None = None) -> list[tuple[int, int, int, int]]:
    skip_ptrs = skip_ptrs or set()
    rects: list[tuple[int, int, int, int]] = []
    for ptr in font.glyph_ptrs:
        if ptr in skip_ptrs:
            continue
        _code, x, y, w, h, _shift, _offset, _tpage = struct.unpack_from("<HHHHHHHH", data, ptr)
        if w <= 0 or h <= 0:
            continue
        rects.append((x, y, x + w + 1, y + h + 1))
    return rects


def _allocate_cjk_font_rects(occupied: list[tuple[int, int, int, int]], count: int,
                             page_w: int, page_h: int) -> list[tuple[int, int, int, int]]:
    best: list[tuple[int, int, int, int]] = []
    for cell_w, cell_h in _CJK_PREFERRED_CELLS:
        placements = _allocate_font_rects(
            occupied, count, 0, 0, page_w, page_h,
            cell_w=cell_w, cell_h=cell_h, padding=1,
        )
        if len(placements) >= count:
            return placements
        if len(placements) > len(best):
            best = placements
    return best


def _allocate_font_rects(occupied: list[tuple[int, int, int, int]], count: int,
                         area_x: int, area_y: int, area_w: int, area_h: int,
                         cell_w: int = 16, cell_h: int = 16,
                         padding: int = 1) -> list[tuple[int, int, int, int]]:
    placements: list[tuple[int, int, int, int]] = []
    occ = list(occupied)
    max_x = area_x + area_w - cell_w
    max_y = area_y + area_h - cell_h
    if max_x < area_x or max_y < area_y:
        return placements
    step_x = cell_w + padding
    step_y = cell_h + padding
    for y in range(area_y, max_y + 1, step_y):
        for x in range(area_x, max_x + 1, step_x):
            rect = (x - padding, y - padding, x + cell_w + padding, y + cell_h + padding)
            if any(_rects_intersect(rect, old) for old in occ):
                continue
            placements.append((x, y, cell_w, cell_h))
            occ.append(rect)
            if len(placements) >= count:
                return placements
    for y in range(area_y, max_y + 1):
        for x in range(area_x, max_x + 1):
            rect = (x - padding, y - padding, x + cell_w + padding, y + cell_h + padding)
            if any(_rects_intersect(rect, old) for old in occ):
                continue
            placements.append((x, y, cell_w, cell_h))
            occ.append(rect)
            if len(placements) >= count:
                return placements
    return placements


def _rects_intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _png_length(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data) or data[offset:offset + 8] != b"\x89PNG\r\n\x1a\n":
        return 0
    pos = offset + 8
    while pos + 12 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        chunk_type = data[pos + 4:pos + 8]
        next_pos = pos + 8 + length + 4
        if next_pos > len(data):
            return 0
        if chunk_type == b"IEND":
            return next_pos - offset
        pos = next_pos
    return 0


def _find_system_cjk_font() -> Path | None:
    try:
        from core.open_source_fonts import ensure_source_han_sans
        font = ensure_source_han_sans()
        if font:
            return font
    except Exception:
        pass
    candidates = [
        Path("C:/Windows/Fonts/SourceHanSansCN-Regular.otf"),
        Path("C:/Windows/Fonts/SourceHanSansSC-Regular.otf"),
        Path("C:/Windows/Fonts/NotoSansCJKsc-Regular.otf"),
        Path("C:/Windows/Fonts/NotoSansSC-Regular.otf"),
        Path("C:/Windows/Fonts/NotoSansSC-VF.ttf"),
    ]
    return next((font for font in candidates if font.exists()), None)


def _load_fitting_font(font_path: Path, ch: str, width: int, height: int):
    try:
        from PIL import ImageFont
    except Exception:
        return None
    best = None
    for size in range(max(6, height + 4), 5, -1):
        try:
            font = ImageFont.truetype(str(font_path), size=size)
        except Exception:
            continue
        bbox = font.getbbox(ch)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        if text_w <= width and text_h <= height:
            return font
        best = font
    return best


registry.register(GameMakerEngine())
