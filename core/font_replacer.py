"""PCK 字体替换 —— 使用内嵌的预导入 CJK 字体，无需 Godot Editor。"""
from __future__ import annotations

import shutil
import re
import struct
from pathlib import Path
from typing import Optional

from core.resources import resource_path
from utils.logger import info, warning

# 内嵌的预导入 CJK 字体 (.fontdata, RSCC 格式)。
# Source Han Sans CN Regular / 思源黑体 CN Regular, SIL Open Font License.
_BUNDLED_FONTDATA = resource_path("assets", "source_han_sans_cn_cjk.fontdata")


def get_bundled_fontdata() -> bytes | None:
    """读取内嵌的 CJK 字体数据。"""
    if _BUNDLED_FONTDATA.exists():
        return _BUNDLED_FONTDATA.read_bytes()
    return None


def build_font_replacement_map(files_in_pck: list[tuple[str, str]]) -> dict[str, bytes]:
    """根据 PCK 中的文件列表，构建字体替换映射。

    Args:
        files_in_pck: [(path, magic4), ...] 只替换 RSCC 格式的字体

    Returns:
        {pck_path: cjk_fontdata}
    """
    cjk_data = get_bundled_fontdata()
    if not cjk_data:
        warning("未找到内嵌 CJK 字体资源，跳过字体替换")
        return {}

    patches = {}
    for path, magic in files_in_pck:
        if path.endswith(".fontdata") and magic == "RSCC":
            patches[path] = cjk_data
            info(f"  字体: {Path(path).name} → CJK")

    return patches


def _iter_standard_pck_entries(data: bytes):
    """Yield (path, absolute_offset, size) for standard front-index Godot PCKs."""
    if data[:4] != b"GDPC" or len(data) < 48:
        return
    file_base = struct.unpack_from("<Q", data, 24)[0]
    if file_base < 48 or file_base > len(data):
        return

    pos = 48
    while pos < file_base and data[pos:pos + 8] == b"\x00" * 8:
        pos += 8

    while pos + 8 < file_base:
        path_len = struct.unpack_from("<I", data, pos + 4)[0]
        if path_len == 0 or path_len > 2000:
            break
        padded = path_len + (4 - path_len % 4) % 4
        entry_payload = pos + 8 + padded
        if entry_payload + 32 > file_base:
            break
        try:
            path = data[pos + 8:pos + 8 + path_len].decode("utf-8", errors="replace").rstrip("\x00")
        except Exception:
            pos = entry_payload + 32
            continue
        entry_offset = struct.unpack_from("<Q", data, entry_payload)[0]
        entry_size = struct.unpack_from("<Q", data, entry_payload + 8)[0]
        absolute = file_base + entry_offset
        if absolute + entry_size <= len(data):
            yield path, absolute, entry_size
        pos = entry_payload + 32


def _pck_entries_with_payloads(pck_path: Path) -> list[tuple[str, int, int, bytes]]:
    data = pck_path.read_bytes()
    if data[:4] != b"GDPC":
        return []
    try:
        from engines.godot_pck import _looks_like_godot_steam_pck, _parse_steam_entries
        if _looks_like_godot_steam_pck(data):
            return [
                (name, offset, size, data[offset:offset + size])
                for name, offset, size, _flags, _md5 in _parse_steam_entries(data)
                if offset + size <= len(data)
            ]
    except Exception:
        pass
    return [
        (name, absolute, size, data[absolute:absolute + size])
        for name, absolute, size in _iter_standard_pck_entries(data)
    ]


def collect_runtime_fontdata_paths_from_pck(pck_path: Path) -> set[str]:
    """Collect imported fontdata paths referenced by runtime Godot resources."""
    entries = _pck_entries_with_payloads(pck_path)
    import_map: dict[str, str] = {}
    runtime_font_sources: set[str] = set()

    import_re = re.compile(r'path="([^"]+\.fontdata)"')
    font_source_re = re.compile(r'res://[^"\x00\r\n]+?\.(?:ttf|otf|ttc)', re.IGNORECASE)
    runtime_resource_exts = (".res", ".scn", ".tscn", ".tres")

    for path, _absolute, _size, payload in entries:
        norm_path = path.replace("\\", "/")
        low = norm_path.lower()
        if low.endswith((".ttf.import", ".otf.import", ".ttc.import")):
            try:
                text = payload.decode("utf-8", errors="replace")
            except Exception:
                continue
            match = import_re.search(text)
            if match:
                import_map[norm_path[:-7]] = match.group(1).replace("\\", "/")
            continue

        if not low.endswith(runtime_resource_exts):
            continue
        if "/editor/" in low or "/example assets/" in low:
            continue
        try:
            text = payload.decode("utf-8", errors="ignore")
        except Exception:
            continue
        for match in font_source_re.finditer(text):
            runtime_font_sources.add(match.group(0).replace("\\", "/"))

    selected = {import_map[source] for source in runtime_font_sources if source in import_map}
    return selected


def build_font_replacement_map_from_pck(
    pck_path: Path,
    selected_paths: set[str] | None = None,
) -> dict[str, bytes]:
    """Build a replacement map for RSCC .fontdata files in a Godot PCK."""
    cjk_data = get_bundled_fontdata()
    if not cjk_data:
        warning("未找到内嵌 CJK 字体资源，跳过字体替换")
        return {}

    patches: dict[str, bytes] = {}
    selected_norm = {p.replace("\\", "/") for p in selected_paths or set()}
    for path, _absolute, size, payload in _pck_entries_with_payloads(pck_path):
        norm_path = path.replace("\\", "/")
        if selected_norm and norm_path not in selected_norm:
            continue
        if norm_path.endswith(".fontdata") and size >= 4 and payload[:4] == b"RSCC":
            patches[norm_path] = cjk_data
            info(f"  字体: {Path(norm_path).name} -> CJK")
    return patches


def replace_fonts_in_pck(
    pck_path: Path,
    output_path: Path,
    build_pck_func=None,
    selected_paths: set[str] | None = None,
) -> bool:
    """替换 PCK 中的字体文件。

    Args:
        pck_path: 原始 PCK 路径
        output_path: 输出 PCK 路径
        build_pck_func: 重建函数 (pck_path, patched_map, output_path)

    Returns:
        True 如果替换成功
    """
    cjk_data = get_bundled_fontdata()
    if not cjk_data:
        return False

    # 解析 PCK 找出所有 RSCC 字体
    data = pck_path.read_bytes()
    if data[:4] != b"GDPC":
        return False

    font_patches = build_font_replacement_map_from_pck(pck_path, selected_paths=selected_paths)

    if not font_patches:
        info("PCK 中没有需要替换的字体文件")
        return False

    if build_pck_func is None:
        from engines.godot_pck import _looks_like_godot_steam_pck, rebuild_pck, rebuild_pck_steam
        build_pck_func = rebuild_pck_steam if _looks_like_godot_steam_pck(data) else rebuild_pck

    info(f"替换 {len(font_patches)} 个字体文件...")
    build_pck_func(pck_path, font_patches, output_path)
    info(f"字体替换完成: {output_path}")
    return True
