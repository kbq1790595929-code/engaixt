"""GodotPckEngine 引擎类：打包 Godot 游戏的检测、提取与回填。"""

from __future__ import annotations

import re
import shutil
import struct
from pathlib import Path

from core.exe_selector import find_main_exe
from engines.base import EngineBase, EngineCapabilities, TextItem
from utils.logger import info, warning
from utils.text_extract import is_translatable

from engines.godot_pck import pck as _pck_mod
from engines.godot_pck.dtl import extract_dtl_text, patch_dtl_content
from engines.godot_pck.pck import (
    _count_files,
    _exe_has_embedded_pck_marker_quick,
    _file_starts_with,
    _looks_like_godot_steam_pck,
    _scn_bytes_contain_japanese,
    rebuild_pck,
    rebuild_pck_steam,
)
from engines.godot_pck.text_formats import (
    _GODOT_BUILTIN_TYPE_NAMES,
    _SKIP_TSCN_PROPS,
    _fit_translation_to_utf8_slot,
    _is_godot_runtime_identifier,
    extract_dialogue_txt,
    extract_tscn_strings,
    patch_dialogue_txt,
    patch_inline_strings_safe,
    patch_tscn_strings,
)


class GodotPckEngine(EngineBase):
    """Engine for packaged Godot games with Dialogic DTL timelines."""

    name = "godot_pck"
    label = "Godot PCK (Dialogic DTL)"
    support_level = "beta"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=False,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "解包并重建 Godot PCK 或替换 exe 内嵌 PCK。",
            "重点支持 Dialogic DTL、场景和资源文本回填。",
        ),
    )
    detect_priority = 94

    DTL_EXT = ".dtl"
    SCN_EXTS = {".scn", ".res"}
    TEXT_EXTS = {".tscn", ".tres"}
    TXT_EXTS = {".txt"}
    _SKIP_EXTS = {".png", ".jpg", ".jpeg", ".ogg", ".mp3", ".wav", ".webp",
                  ".svg", ".ttf", ".otf", ".woff", ".woff2", ".ico", ".import"}
    _SKIP_SCN_NAME_PARTS = (
        "timeline_editor",
        "reference_manager",
        "char_edit",
        "character_editor",
        "settings_",
        "branch",
        "character_prefix_suffix",
        "portrait_scene_browser",
    )

    def detect(self, path: Path) -> bool:
        game_dir = path if path.is_dir() else path.parent

        for sub in ["", "contents", "game", "data", "pack"]:
            search = game_dir / sub if sub else game_dir
            if not search.is_dir():
                continue
            for pck in search.glob("*.pck"):
                if _file_starts_with(pck, b"GDPC"):
                    return True
            # Also check EXE for embedded PCK
            for exe in search.glob("*.exe"):
                if _exe_has_embedded_pck_marker_quick(exe):
                    return True
        return False

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        for sub in ["", "contents", "game", "data", "pack"]:
            search = game_dir / sub if sub else game_dir
            if not search.is_dir():
                continue
            exe = find_main_exe(search, recursive=False)
            if exe:
                return exe
            parent = search.parent
            if parent != search:
                exe = find_main_exe(parent, recursive=False)
                if exe:
                    return exe
        return None

    # ------------------------------------------------------------------
    # Unpack
    # ------------------------------------------------------------------

    @staticmethod
    def _select_main_pck(pcks: list[Path]) -> Path:
        """Select the best PCK for text extraction.

        Priority: largest non-translation PCK > largest overall.
        Translation packs (translation_*.pck) contain only localized strings
        and won't have the game's original text to translate.
        """
        non_trans = [p for p in pcks if not p.name.startswith("translation_")]
        candidates = non_trans if non_trans else pcks
        return max(candidates, key=lambda p: p.stat().st_size)

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        self._game_dir = game_dir
        self._pck_steam = False
        self._last_patched_files = {}
        self._last_deploy_mode = ""
        self._last_output_pck = None
        self._last_effective_translations = {}
        self._deploy_pck_path = None

        # Find PCK (standalone or embedded in EXE)
        pck_path = None
        self._embedded_exe = None
        self._embedded_pck_range = None

        for sub in ["contents", "game", "data", "pack", ""]:
            search = game_dir / sub if sub else game_dir
            if not search.is_dir():
                continue
            pcks = list(search.glob("*.pck"))
            if pcks:
                deploy_pck = self._select_main_pck(pcks)
                backup_pck = deploy_pck.with_suffix(".pck.pre_tool")
                pck_path = backup_pck if backup_pck.exists() else deploy_pck
                self._pck_path = pck_path
                self._deploy_pck_path = deploy_pck
                self._pck_sub = sub
                self._embedded_exe = None
                break
            # Check for embedded PCK in EXE
            exes = list(search.glob("*.exe"))
            for exe in exes:
                try:
                    exe_data = exe.read_bytes()
                    pck_range = _pck_mod._find_embedded_pck_range(exe_data)
                    if pck_range is not None:
                        # Extract PCK from EXE
                        gdpc_idx, pck_end = pck_range
                        pck_data = exe_data[gdpc_idx:pck_end]
                        pck_path = workspace / "embedded.pck"
                        pck_path.write_bytes(pck_data)
                        self._pck_path = pck_path
                        self._embedded_exe = exe
                        self._embedded_pck_range = pck_range
                        break
                except Exception:
                    pass
            if pck_path:
                break

        if not pck_path:
            warning("未找到 Godot PCK 文件")
            return []

        if self._embedded_exe:
            info(f"从 EXE 提取嵌入 PCK: {self._embedded_exe.name} ({pck_path.stat().st_size:,} bytes)")
        else:
            deploy_pck = getattr(self, "_deploy_pck_path", None)
            if deploy_pck and pck_path != deploy_pck:
                info(f"解包 PCK 原始备份: {pck_path.name} -> {deploy_pck.name}")
            else:
                info(f"解包 PCK: {pck_path.name}")

        # Extract PCK to workspace
        extract_dir = workspace / "original"
        extract_dir.mkdir(parents=True, exist_ok=True)
        self._extract_pck(pck_path, extract_dir)

        # Save for repack
        self._extract_dir = extract_dir

        # Extract text from DTL and SCN/RES files
        items: list[TextItem] = []
        dtl_count = 0
        scn_count = 0
        csv_count = 0

        for root, dirs, files in extract_dir.walk():
            for f in files:
                ext = Path(f).suffix.lower()
                filepath = root / f
                try:
                    size = filepath.stat().st_size
                except OSError:
                    continue
                if size < 10 or size > 5 * 1024 * 1024:
                    continue

                rel = str(filepath.relative_to(extract_dir))

                if ext == self.DTL_EXT:
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    file_items = extract_dtl_text(content, rel)
                    items.extend(file_items)
                    dtl_count += len(file_items)

                elif ext in self.TXT_EXTS:
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    file_items = extract_dialogue_txt(content, rel)
                    items.extend(file_items)
                    dtl_count += len(file_items)

                elif ext == ".csv":
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    file_items = self._extract_csv(content, rel)
                    items.extend(file_items)
                    csv_count += len(file_items)

                elif ext in self.SCN_EXTS:
                    try:
                        data = filepath.read_bytes()
                    except Exception:
                        continue
                    file_items = self._extract_scn_strings(data, rel)
                    items.extend(file_items)
                    scn_count += len(file_items)

                elif ext in self.TEXT_EXTS:
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    file_items = extract_tscn_strings(content, rel)
                    items.extend(file_items)
                    scn_count += len(file_items)

        info(f"DTL: {dtl_count} 条, SCN/RES/TSCN: {scn_count} 条, CSV: {csv_count} 条, 共 {len(items)} 条")
        return items

    # ------------------------------------------------------------------
    # Repack
    # ------------------------------------------------------------------

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        self._last_patched_files = {}
        self._last_deploy_mode = ""
        self._last_output_pck = None

        items = self.filter_repack_items(items)
        if not items:
            return

        translated = [it for it in items if it.translated and it.translated != it.original]
        if not translated:
            info("没有需要回填的翻译")
            return

        from config import get_config
        config = get_config()

        pck_path = getattr(self, "_pck_path", None)
        extract_dir = getattr(self, "_extract_dir", None)
        if not pck_path or not extract_dir:
            warning("缺少 PCK 路径或解包目录，无法回填")
            return

        info(f"回填 {len(translated)} 条翻译到 PCK...")

        # Group translations by file
        dtl_trans: dict[str, dict[str, str]] = {}
        scn_trans: dict[str, dict[str, str]] = {}
        text_trans: dict[str, dict[str, str]] = {}
        csv_trans: dict[str, dict[str, str]] = {}

        for item in translated:
            ext = Path(item.file).suffix.lower()
            if ext == self.DTL_EXT:
                dtl_trans.setdefault(item.file, {})[item.original] = item.translated
            elif ext in self.TXT_EXTS:
                dtl_trans.setdefault(item.file, {})[item.original] = item.translated
            elif ext == ".csv":
                csv_trans.setdefault(item.file, {})[item.original] = item.translated
            elif ext in self.SCN_EXTS:
                scn_trans.setdefault(item.file, {})[item.original] = item.translated
            elif ext in self.TEXT_EXTS:
                text_trans.setdefault(item.file, {})[item.original] = item.translated

        # Build patched map for PCK rebuilding
        all_patched: dict[str, bytes] = {}
        effective_translations: dict[str, dict[str, str]] = {}
        _norm = lambda p: p.replace("\\", "/")

        # Patch DTL and TXT files
        dtl_count = 0
        for rel_path, trans_map in dtl_trans.items():
            target = extract_dir / rel_path
            if not target.exists():
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            ext = Path(rel_path).suffix.lower()
            if ext == self.DTL_EXT:
                patched, n = patch_dtl_content(content, trans_map)
            else:
                patched, n = patch_dialogue_txt(content, trans_map)
            if n > 0:
                norm_rel = _norm(rel_path)
                all_patched[norm_rel] = patched.encode("utf-8")
                effective_translations[norm_rel] = dict(trans_map)
                dtl_count += n
        info(f"DTL/TXT: {dtl_count} 处替换 ({len(all_patched)} 个文件)")

        # Patch CSV files
        csv_replaced = 0
        for rel_path, trans_map in csv_trans.items():
            target = extract_dir / rel_path
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            patched, n = self._patch_csv(content, trans_map)
            if n > 0:
                norm_rel = _norm(rel_path)
                all_patched[norm_rel] = patched.encode("utf-8")
                effective_translations[norm_rel] = dict(trans_map)
                csv_replaced += n
        if csv_replaced > 0:
            info(f"CSV: {csv_replaced} 处替换 ({len([k for k in csv_trans if k])} 个文件)")

        # Patch SCN/RES files — padding shorter, skipping longer
        scn_shorter = 0
        scn_longer = 0
        scn_equal = 0
        scn_files_patched = 0
        for rel_path, trans_map in scn_trans.items():
            target = extract_dir / rel_path
            if not target.exists():
                continue
            data = target.read_bytes()
            orig_size = len(data)
            effective_map = {}
            for original, translated in trans_map.items():
                fitted = _fit_translation_to_utf8_slot(
                    original, translated, len(original.encode("utf-8"))
                )
                if fitted:
                    effective_map[original] = fitted
            patched, r_short, r_long, r_eq = patch_inline_strings_safe(data, trans_map)
            if r_short + r_long + r_eq > 0:
                if len(patched) != orig_size:
                    warning(f"  文件大小变化: {rel_path} ({orig_size} -> {len(patched)}, "
                            f"差{len(patched) - orig_size} bytes)")
                norm_rel = _norm(rel_path)
                all_patched[norm_rel] = patched
                effective_translations[norm_rel] = effective_map
                scn_shorter += r_short
                scn_longer += r_long
                scn_equal += r_eq
                scn_files_patched += 1
        info(f"SCN/RES: {scn_equal} 处替换 ({scn_files_patched} 个文件)")

        # Patch TSCN/TRES files — variable-length replacement (text format, safe)
        text_count = 0
        text_files_patched = 0
        for rel_path, trans_map in text_trans.items():
            target = extract_dir / rel_path
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            patched, n = patch_tscn_strings(content, trans_map)
            if n > 0:
                norm_rel = _norm(rel_path)
                all_patched[norm_rel] = patched.encode("utf-8")
                effective_translations[norm_rel] = dict(trans_map)
                text_count += n
                text_files_patched += 1
        if text_files_patched > 0:
            info(f"TSCN/TRES: {text_count} 处替换 ({text_files_patched} 个文件)")

        # Font replacement
        cjk_font = config.cjk_font_path
        if cjk_font:
            cjk_ttf = Path(cjk_font)
            if cjk_ttf.exists():
                font_patches = self._build_font_patches(pck_path, cjk_ttf, extract_dir)
                all_patched.update(font_patches)
                info(f"字体: {len(font_patches)} 个替换")

        if not all_patched:
            info("没有需要修改的内容")
            return

        self._last_patched_files = dict(all_patched)
        self._last_effective_translations = effective_translations

        # Rebuild PCK and deploy patched files.
        # If PCK rebuilding produces an invalid (too small) result, fall back
        # to writing patched files as loose overrides alongside the game.
        output_pck = workspace / "patched.pck"
        self._last_output_pck = output_pck
        info(f"重建 PCK: {len(all_patched)} 个文件已修改")
        rebuild_ok = True
        try:
            if getattr(self, "_pck_steam", False):
                rebuild_pck_steam(pck_path, all_patched, output_pck, extract_dir)
            else:
                rebuild_pck(pck_path, all_patched, output_pck)
        except Exception as exc:
            warning(f"PCK 重建失败，使用松散文件回退: {exc}")
            rebuild_ok = False

        # Validate rebuilt PCK
        if rebuild_ok and output_pck.exists():
            pck_size = output_pck.stat().st_size
            orig_size = pck_path.stat().st_size
            if pck_size < orig_size * 0.01:  # < 1% of original → broken
                warning(f"PCK 重建异常 (大小 {pck_size:,} vs 原始 {orig_size:,})，使用松散文件回退")
                rebuild_ok = False

        if rebuild_ok:
            self._last_deploy_mode = "pck"
            self._deploy_rebuilt_pck(pck_path, output_pck, all_patched)
        else:
            self._last_deploy_mode = "loose"
            self._deploy_loose_files(pck_path, all_patched)

    def filter_repack_items(self, items: list[TextItem]) -> list[TextItem]:
        """Drop Godot runtime identifiers from external checkpoints before repack."""
        filtered: list[TextItem] = []
        skipped = 0
        for item in items:
            if _is_godot_runtime_identifier(item.original):
                skipped += 1
                continue
            if item.original in _GODOT_BUILTIN_TYPE_NAMES:
                skipped += 1
                continue

            ext = Path(item.file).suffix.lower()
            if ext in self.TEXT_EXTS and ":" in item.key:
                prop = item.key.rsplit(":", 1)[-1]
                if prop in _SKIP_TSCN_PROPS:
                    skipped += 1
                    continue

            filtered.append(item)

        if skipped:
            warning(f"Godot: 跳过 {skipped} 条运行时标识符/资源属性翻译")
        return filtered

    def _deploy_rebuilt_pck(self, pck_path, output_pck, all_patched):
        """Copy the rebuilt PCK to the game directory."""
        embedded_exe = getattr(self, "_embedded_exe", None)
        if embedded_exe:
            exe_data = embedded_exe.read_bytes()
            pck_range = _pck_mod._find_embedded_pck_range(exe_data)
            if pck_range is None:
                warning("EXE 中未找到 PCK 嵌入位置")
                return
            gdpc_idx, pck_end = pck_range
            exe_part = exe_data[:gdpc_idx]
            exe_tail = exe_data[pck_end:]
            new_pck_data = output_pck.read_bytes()
            combined = exe_part + new_pck_data + exe_tail

            backup = embedded_exe.with_suffix(".exe.pre_tool")
            if not backup.exists():
                shutil.copy2(embedded_exe, backup)
                info(f"已备份原始 EXE: {backup.name}")

            game_dir = getattr(self, "_game_dir", embedded_exe.parent)
            target_exe = game_dir / embedded_exe.name if game_dir != embedded_exe.parent else embedded_exe
            target_exe.write_bytes(combined)
            info(f"EXE 已更新 (嵌入 PCK): {embedded_exe.name} ({len(combined):,} bytes)")
        else:
            game_pck = getattr(self, "_deploy_pck_path", None) or pck_path
            backup = game_pck.with_suffix(".pck.pre_tool")
            if not backup.exists() and game_pck.exists():
                shutil.copy2(game_pck, backup)
                info(f"已备份原始 PCK: {backup.name}")
            shutil.copy2(output_pck, game_pck)
            info(f"PCK 已更新: {game_pck.name} ({output_pck.stat().st_size:,} bytes)")

    def _deploy_loose_files(self, pck_path, all_patched):
        """Write patched files as loose overrides alongside the PCK.

        Godot loads loose files with higher priority than PCK contents,
        so this effectively patches the game without modifying the PCK.
        """
        game_dir = getattr(self, "_game_dir", pck_path.parent)
        if not game_dir or not game_dir.is_dir():
            game_dir = pck_path.parent

        copied = 0
        for rel_path, data in all_patched.items():
            dest = game_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            copied += 1

        # Also backup original PCK for rollback
        deploy_pck = getattr(self, "_deploy_pck_path", None) or pck_path
        backup = deploy_pck.with_suffix(".pck.pre_tool")
        if not backup.exists() and deploy_pck.exists():
            shutil.copy2(deploy_pck, backup)
            info(f"已备份原始 PCK: {backup.name}")

        info(f"松散文件部署: {copied} 个文件覆盖到 {game_dir}")

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    def _extract_pck(self, pck_path: Path, dest: Path):
        """Extract PCK entries to destination directory.

        Handles both standard Godot PCK (entries at beginning after header)
        and GodotSteam embedded format (entries at end before footer).
        """
        data = pck_path.read_bytes()
        if data[:4] != b"GDPC":
            raise ValueError("不是有效的 Godot PCK")

        self._pck_steam = _looks_like_godot_steam_pck(data)
        if self._extract_pck_tool(pck_path, dest):
            info("  解包格式: " + ("GodotSteam" if self._pck_steam else "standard"))
            return

        file_base = struct.unpack_from("<Q", data, 24)[0]
        skipped = 0
        extracted = 0
        steam = False

        if self._pck_steam:
            extracted, skipped = self._extract_pck_steam(data, dest)
            info(f"  解包: {extracted} 个文件, 跳过 {skipped} 个 (GodotSteam)")
            return

        # --- Try standard forward entry parsing (entries at beginning) ---
        pos = 48
        while pos < file_base:
            if pos + 8 > file_base:
                break
            path_len = struct.unpack_from("<I", data, pos + 4)[0]
            if path_len == 0 or path_len > 2000:
                pos += 1
                continue
            padded = path_len + (4 - path_len % 4) % 4
            ep = pos + 4 + 4 + padded
            if ep + 32 > file_base:
                break
            path_bytes = data[pos + 8:pos + 8 + path_len]
            try:
                file_path = path_bytes.decode("utf-8", errors="replace").rstrip("\x00")
            except UnicodeDecodeError:
                pos = ep + 32
                continue

            entry_offset = struct.unpack_from("<Q", data, ep)[0]
            entry_size = struct.unpack_from("<Q", data, ep + 8)[0]
            abs_offset = file_base + entry_offset

            ext = Path(file_path).suffix.lower()
            if ext in self._SKIP_EXTS or entry_size > 10 * 1024 * 1024:
                skipped += 1
                pos = ep + 32
                continue

            if abs_offset + entry_size <= len(data):
                file_data = data[abs_offset:abs_offset + entry_size]
                dest_file = dest / file_path
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(file_data)
                extracted += 1
            pos = ep + 32

        # --- If standard parsing found nothing, try GodotSteam format ---
        if extracted == 0 and self._pck_steam:
            footer_data_size = struct.unpack_from("<Q", data, len(data) - 12)[0]
            if file_base < footer_data_size < len(data):
                steam = True
                extracted, skipped = self._extract_pck_steam(data, dest)

        info(f"  解包: {extracted} 个文件, 跳过 {skipped} 个"
             + (" (GodotSteam)" if steam else ""))

    def _extract_pck_tool(self, pck_path: Path, dest: Path) -> bool:
        """Try external GDRE tools before the built-in parser.

        GDRE command-line flags have changed across releases, so this probes a
        few known forms and accepts the first one that actually writes files.
        """
        try:
            from core.tool_manager import find_tool, ensure_tool
        except Exception:
            return False

        candidates = []
        for tool_name in ("gdsdecomp", "gdre_tools"):
            tool = find_tool(tool_name)
            if tool and tool not in candidates:
                candidates.append(tool)
        if not candidates:
            for tool_name in ("gdsdecomp", "gdre_tools"):
                tool = ensure_tool(tool_name)
                if tool and tool not in candidates:
                    candidates.append(tool)

        for exe in candidates:
            commands = [
                [str(exe), "--headless", f"--extract={pck_path}", f"--output={dest}"],
                [str(exe), "--headless", f"--recover={pck_path}", f"--output={dest}"],
                [str(exe), str(pck_path), "-o", str(dest)],
            ]
            for cmd in commands:
                try:
                    before = _count_files(dest)
                    result = __import__("subprocess").run(
                        cmd, capture_output=True, text=True, timeout=180
                    )
                    after = _count_files(dest)
                    if result.returncode == 0 and after > before:
                        info(f"  使用 {Path(exe).name} 解包成功")
                        return True
                except Exception:
                    continue
        return False

    def _extract_pck_steam(self, data: bytes, dest: Path) -> tuple[int, int]:
        """Extract files from GodotSteam embedded PCK format.

        Entries are stored at the END of the PCK, before the footer
        (data_size + GDPC). Walk backwards from the footer.
        Entry format: name_len(4) + name(N) + pad_to_4 + offset(8) + size(8) + md5(16) + flags(4)
        """
        file_base = struct.unpack_from("<Q", data, 24)[0]
        footer_start = len(data) - 12  # position of data_size field (8 bytes before GDPC)
        data_size_val = struct.unpack_from("<Q", data, footer_start)[0]

        pos = footer_start  # end of last entry
        entries = []  # [(name, offset, size), ...]
        failed = 0

        while pos > file_base and failed < 5:
            meta_start = pos - 36
            if meta_start < 0:
                break

            foffset = struct.unpack_from("<Q", data, meta_start)[0]
            fsize = struct.unpack_from("<Q", data, meta_start + 8)[0]

            if foffset >= data_size_val or fsize > 500_000_000:
                failed += 1
                break

            # Find name_len by scanning backwards in multiples of 4
            found = False
            for padded_block in range(4, 2048, 4):
                name_len_pos = meta_start - padded_block
                if name_len_pos < 0:
                    break
                potential_len = struct.unpack_from("<I", data, name_len_pos)[0]
                if potential_len < 1 or potential_len > 1000:
                    continue
                expected_padded = (4 + potential_len + 3) // 4 * 4
                if expected_padded != padded_block:
                    continue
                name_start = name_len_pos + 4
                name_end = name_start + potential_len
                if name_end > meta_start:
                    continue
                padding = data[name_end:meta_start]
                if not all(b == 0 for b in padding):
                    continue
                try:
                    name = data[name_start:name_end].rstrip(b'\x00').decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if not name:
                    continue
                entries.append((name, foffset, fsize))
                pos = name_len_pos
                found = True
                break

            if not found:
                failed += 1
                break

        # Extract files (entries were collected in reverse order)
        extracted = 0
        skipped = 0
        for name, foffset, fsize in entries:
            abs_offset = foffset  # GodotSteam offsets are relative to PCK start
            ext = Path(name).suffix.lower()
            if ext in self._SKIP_EXTS or fsize > 10 * 1024 * 1024:
                skipped += 1
                continue
            if abs_offset + fsize <= len(data):
                dest_file = dest / name
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                dest_file.write_bytes(data[abs_offset:abs_offset + fsize])
                extracted += 1

        return extracted, skipped

    # ------------------------------------------------------------------
    # CSV extraction / patching (Godot CSV translation format)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_csv(content: str, rel_path: str) -> list[TextItem]:
        """Extract translatable text from Godot CSV translation files.

        Format: ,en (header row), then key,text rows.
        Uses csv module for proper multi-line quoted field handling.
        """
        import csv as _csv
        import io as _io

        items = []
        try:
            reader = _csv.reader(_io.StringIO(content))
            rows = list(reader)
        except Exception:
            return items

        for row_idx, row in enumerate(rows):
            if row_idx == 0:  # skip header
                continue
            if not row or len(row) < 2:
                continue
            key = row[0].strip()
            en_text = row[1].strip()
            if en_text and is_translatable(en_text) and len(en_text) > 1:
                items.append(TextItem(
                    file=rel_path,
                    key=key or f"row_{row_idx}",
                    original=en_text,
                    line=row_idx + 1,
                ))
        return items

    @staticmethod
    def _patch_csv(content: str, translations: dict[str, str]) -> tuple[str, int]:
        """Apply translations to CSV content. Returns (patched_content, count)."""
        import csv as _csv
        import io as _io

        try:
            reader = _csv.reader(_io.StringIO(content))
            rows = list(reader)
        except Exception:
            return content, 0

        replaced = 0
        for row in rows:
            if len(row) < 2:
                continue
            en_text = row[1].strip()
            if en_text and en_text in translations:
                trans = translations[en_text]
                if trans and trans != en_text:
                    row[1] = trans
                    replaced += 1

        output = _io.StringIO()
        writer = _csv.writer(output, lineterminator="\n")
        writer.writerows(rows)
        return output.getvalue(), replaced

    # ------------------------------------------------------------------
    # SCN/RES extraction
    # ------------------------------------------------------------------

    def _extract_scn_strings(self, data: bytes, rel_path: str) -> list[TextItem]:
        """Extract translatable VARIANT_STRING values from binary SCN/RES.

        Filters out Godot internal identifiers: resource UIDs, type names,
        enum values, and other non-user-facing strings.
        Skips addons/ directory files entirely (editor UI, not game text).
        """
        # Skip addon/editor files. Dialogic editor resources can be exported
        # under .godot/exported even though they are not user-facing game text.
        norm_rel = rel_path.replace("\\", "/").lower()
        if norm_rel.startswith("addons/"):
            return []
        exported_editor_resource = (
            ".godot/exported/" in norm_rel
            and any(part in norm_rel for part in self._SKIP_SCN_NAME_PARTS)
        )
        if exported_editor_resource and not _scn_bytes_contain_japanese(data):
            return []

        items = []
        pos = 0
        while pos + 12 <= len(data):
            try:
                vtype = struct.unpack_from("<I", data, pos)[0]
            except struct.error:
                break
            if vtype == 5:  # VARIANT_STRING
                try:
                    slen = struct.unpack_from("<I", data, pos + 4)[0]
                except struct.error:
                    break
                if slen > 0 and slen < 10000 and pos + 8 + slen <= len(data):
                    sdata = data[pos + 8:pos + 8 + slen]
                    if sdata[-1] == 0:  # null-terminated
                        try:
                            s = sdata[:-1].decode("utf-8")
                        except UnicodeDecodeError:
                            pos += 8 + slen
                            continue
                        # Skip Godot internal identifiers
                        if self._is_scn_string_translatable(s):
                            items.append(TextItem(
                                file=rel_path, key=f"var_{pos}", original=s,
                            ))
                        pos += 8 + slen
                        continue
                    pos += 1
                else:
                    pos += 1
            else:
                pos += 1
        return items

    @staticmethod
    def _is_scn_string_translatable(s: str) -> bool:
        """Check if a string from SCN/RES is user-facing text (not a Godot internal)."""
        if not s or not s.strip():
            return False

        # Contains null bytes or non-text control characters → binary data.
        # Newlines/tabs are valid in Godot labels and rich text.
        if "\x00" in s or any(0 < ord(c) < 32 and c not in "\n\r\t" for c in s):
            return False

        # Godot resource UIDs: uid://xxx
        if s.startswith("uid://"):
            return False

        # Godot resource paths: res://xxx
        if s.startswith("res://"):
            return False

        if _is_godot_runtime_identifier(s):
            return False

        # Godot 4 built-in class/type names that appear as bare strings in
        # binary resources (e.g. ext_resource "type" field). Translating them
        # corrupts the ResourceLoader type tag and causes load failures.
        if s in _GODOT_BUILTIN_TYPE_NAMES:
            return False

        # ALL_UPPERCASE identifiers of any length (SE, OK, INLINE, RANDOM, etc.)
        if re.match(r"^[A-Z][A-Z0-9_]*$", s):
            return False

        # Purely ASCII strings can be game text (English-original games) or
        # Godot engine internals. Apply stricter filters below instead of
        # rejecting all ASCII. The caller's is_translatable() does final check.

        # Section/scene IDs like "02_A", "03_B", "06_B1"
        if re.match(r"^\d{2}_[A-Z][A-Z0-9]?$", s):
            return False

        # Timeline/event reference IDs such as chapter + branch + event suffix.
        if re.match(r"^\d{2}_[A-Z][A-Z0-9]?_\w+$", s):
            return False

        # Scene/timeline filename references such as TitleCase_Name_01.
        if re.match(r"^[A-Z][a-z]+\w*_\d+$", s):
            return False

        # Single character (F, 0, etc.) — never translatable
        stripped = s.strip()
        if len(stripped) == 1:
            return False

        # Pure numeric or punctuation
        if not re.search(r"[a-zA-Z一-鿿぀-ゟ゠-ヿ]", s):
            return False

        # Delegate to shared is_translatable for remaining checks. Validate a
        # whitespace-normalized copy so multiline labels are not rejected as
        # raw binary/control data, while preserving the original string item.
        return is_translatable(re.sub(r"[\r\n\t]+", " ", s))

    def _build_font_patches(self, pck_path: Path, cjk_ttf: Path,
                            extract_dir: Path) -> dict[str, bytes]:
        """Build font replacement map using bundled CJK fontdata."""
        from core.font_replacer import get_bundled_fontdata

        cjk_data = get_bundled_fontdata()
        if not cjk_data:
            warning("未找到内嵌 CJK 字体资源，跳过字体替换")
            return {}

        patches = {}
        imported_dir = extract_dir / ".godot" / "imported"
        if imported_dir.is_dir():
            for fd in imported_dir.glob("*.fontdata"):
                try:
                    magic = fd.read_bytes()[:4]
                except Exception:
                    continue
                if magic == b"RSCC":
                    rel = str(fd.relative_to(extract_dir)).replace("\\", "/")
                    patches[rel] = cjk_data
                    info(f"  字体: {fd.name} → CJK")

        return patches
