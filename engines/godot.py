"""Godot Engine 支持 — PCK 解包、场景/脚本文本提取与翻译回写"""
from __future__ import annotations

import os
import re
import struct
import shutil
import subprocess
from pathlib import Path

from core.exe_selector import find_main_exe
from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, warning, debug
from utils.text_extract import is_translatable


class GodotEngine(EngineBase):
    name = "godot"
    label = "Godot 引擎"
    support_level = "partial"
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
            "处理已解包 Godot 明文工程资源。",
            "封包游戏优先交给 Godot PCK 或 Godot Frida 路线。",
        ),
    )
    detect_priority = 75

    # 需要跳过的大文件/非文本扩展
    _SKIP_EXTS = {".png", ".jpg", ".jpeg", ".ogg", ".mp3", ".wav", ".webp", ".svg",
                  ".ttf", ".otf", ".woff", ".woff2", ".ico", ".icns",
                  ".import", ".sample", ".remap", ".res", ".scn"}

    # Godot 文本类扩展
    _TEXT_EXTS = {".tscn", ".gd", ".tres", ".tscn.remap", ".tres.remap",
                  ".txt", ".json", ".cfg", ".csv", ".po", ".translation"}

    # 需要跳过的目录
    _SKIP_DIRS = {".godot", ".import", "__pycache__", ".git", "fonts", "shaders",
                  "assets", "images", "audio", "music", "sounds", "sfx", "bgm"}

    def detect(self, path: Path) -> bool:
        if path.is_file():
            path = path.parent

        # 检查 .pck 文件
        if list(path.glob("*.pck")):
            return True

        # 检查 Godot 项目文件
        if (path / "project.godot").is_file():
            return True

        # 检查子目录中的 Godot 结构（如 game/、contents/、pack/）
        for sub in ["contents", "game", "data", "pack"]:
            if (path / sub / "project.godot").is_file():
                return True
            if list((path / sub).glob("*.pck")):
                return True

        return False

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = self._find_game_dir(path)

        # Step 1: 解包 .pck（如果存在）
        pck_files = list(game_dir.glob("*.pck"))
        extract_dir = workspace / "original"
        extract_dir.mkdir(parents=True, exist_ok=True)

        if pck_files:
            pck_path = pck_files[0]
            info(f"解包 PCK: {pck_path.name}")
            self._extract_pck(pck_path, extract_dir)

            # 如果有 .pck，找到解包后的内容目录
            # Godot PCK 通常包含 .godot/ 目录，文件在项目根
            source_dirs = [extract_dir]
            for d in [extract_dir / "contents", extract_dir / "game", extract_dir / "data"]:
                if d.is_dir():
                    source_dirs.append(d)
        else:
            source_dirs = [game_dir]
            # 复制项目文件到工作区
            for root, dirs, files in game_dir.walk():
                dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
                rel = root.relative_to(game_dir)
                dest = extract_dir / rel
                for f in files:
                    ext = Path(f).suffix.lower()
                    if ext in self._SKIP_EXTS:
                        continue
                    src_file = root / f
                    if src_file.stat().st_size > 10 * 1024 * 1024:
                        continue
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest / f)

        # Step 2: 扫描文本文件
        items: list[TextItem] = []
        for sd in source_dirs:
            if not sd.is_dir():
                continue
            for root, dirs, files in sd.walk():
                dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
                for f in files:
                    ext = Path(f).suffix.lower()
                    if ext not in self._TEXT_EXTS:
                        continue
                    filepath = Path(root) / f
                    try:
                        size = filepath.stat().st_size
                    except OSError:
                        continue
                    if size < 10 or size > 5 * 1024 * 1024:
                        continue

                    rel = str(filepath.relative_to(extract_dir))
                    try:
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue

                    if ext == ".tscn" or ext.endswith(".tscn.remap"):
                        items.extend(self._extract_tscn(content, rel))
                    elif ext == ".gd":
                        items.extend(self._extract_gd(content, rel))
                    elif ext == ".tres" or ext.endswith(".tres.remap"):
                        items.extend(self._extract_tscn(content, rel))  # 同格式
                    elif ext == ".csv":
                        items.extend(self._extract_csv(content, rel))
                    elif ext == ".translation" or ext == ".po":
                        items.extend(self._extract_po(content, rel))
                    else:
                        items.extend(self._extract_generic(content, rel))

        info(f"提取到 {len(items)} 条可翻译文本（来自 Godot 项目）")
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        if not items:
            return

        # 只回填已翻译的条目
        translated = [it for it in items if it.translated and it.translated != it.original]
        if not translated:
            info("没有需要回填的翻译")
            return

        original_dir = workspace / "original"

        # 按文件分组
        files: dict[str, list[TextItem]] = {}
        for item in translated:
            files.setdefault(item.file, []).append(item)

        for filename, file_items in files.items():
            target = original_dir / filename
            if not target.exists():
                warning(f"文件不存在: {target}")
                continue

            content = target.read_text(encoding="utf-8", errors="replace")

            for item in file_items:
                if item.original in content:
                    content = content.replace(item.original, item.translated, 1)

            target.write_text(content, encoding="utf-8")
            info(f"回填: {filename} ({len(file_items)} 条)")

        info(f"Godot 回填完成: {len(files)} 个文件")

    def find_exe(self, path: Path) -> Path | None:
        game_dir = self._find_game_dir(path)
        return find_main_exe(game_dir, recursive=False) or find_main_exe(game_dir.parent, recursive=False)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _find_game_dir(self, path: Path) -> Path:
        """找到游戏的实际内容目录。"""
        if path.is_file():
            path = path.parent
        for sub in ["contents", "game", "data"]:
            d = path / sub
            if d.is_dir() and (list(d.glob("*.pck")) or (d / "project.godot").is_file()):
                return d
        return path

    def _extract_pck(self, pck_path: Path, dest: Path):
        """解包 Godot 4 PCK 文件到目标目录。"""
        # 优先使用外部工具
        if self._extract_pck_tool(pck_path, dest):
            return

        # 内置 Python PCK 解包
        info("使用内置 PCK 解析器...")
        try:
            entries = self._read_pck_entries(pck_path)
            with open(pck_path, "rb") as f:
                for file_path, offset, size in entries:
                    # 跳过 .import 和非文本大文件
                    ext = Path(file_path).suffix.lower()
                    if ext in self._SKIP_EXTS or size > 10 * 1024 * 1024:
                        continue

                    dest_file = dest / file_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    f.seek(offset)
                    dest_file.write_bytes(f.read(size))
            info(f"  PCK 解包完成: {len(entries)} 个文件")
        except Exception as e:
            warning(f"PCK 解包失败: {e}")

    def _extract_pck_tool(self, pck_path: Path, dest: Path) -> bool:
        """尝试使用外部 PCK 解包工具。"""
        try:
            from core.tool_manager import find_tool, ensure_tool
        except Exception:
            find_tool = None
            ensure_tool = None

        # 尝试 godotpcktool (pip install godotpcktool)
        try:
            result = subprocess.run(
                ["godotpcktool", str(pck_path), "--extract", str(dest)],
                capture_output=True, timeout=60,
            )
            if result.returncode == 0:
                info("  使用 godotpcktool 解包成功")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # 尝试 GDRE/gdsdecomp 系列。不同版本 CLI 参数差异较大，
        # 所以多试几组常见参数，任一成功或产生文件即视为可用。
        gdre_candidates = []
        if find_tool:
            for tool_name in ("gdsdecomp", "gdre_tools"):
                tool = find_tool(tool_name)
                if tool and tool not in gdre_candidates:
                    gdre_candidates.append(tool)
        if ensure_tool and not gdre_candidates:
            for tool_name in ("gdsdecomp", "gdre_tools"):
                tool = ensure_tool(tool_name)
                if tool and tool not in gdre_candidates:
                    gdre_candidates.append(tool)

        for exe in gdre_candidates:
            commands = [
                [str(exe), "--headless", f"--extract={pck_path}", f"--output={dest}"],
                [str(exe), "--headless", f"--recover={pck_path}", f"--output={dest}"],
                [str(exe), str(pck_path), "-o", str(dest)],
            ]
            for cmd in commands:
                try:
                    before = _count_files(dest)
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                    after = _count_files(dest)
                    if result.returncode == 0 and after > before:
                        info(f"  使用 {Path(exe).name} 解包成功")
                        return True
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    continue

        return False

    def _read_pck_entries(self, pck_path: Path) -> list[tuple[str, int, int]]:
        """解析 Godot 4 PCK 文件索引。"""
        entries = []
        with open(pck_path, "rb") as f:
            header = f.read(48)
            if len(header) < 48 or header[:4] != b"GDPC":
                raise ValueError("不是有效的 Godot PCK 文件")

            # PCK header 结构 (Godot 4):
            # 0-3:   magic "GDPC"
            # 4-7:   version (major, minor, patch, pack)
            # 8-15:  reserved
            # 16-23: flags, count
            # 24-31: base offset (uint64 LE)
            # 32-47: reserved
            file_base = struct.unpack_from("<Q", header, 24)[0]

            pos = 48
            f.seek(48)

            while pos < file_base:
                buf = f.read(4)
                if len(buf) < 4:
                    break
                path_len = struct.unpack("<I", buf)[0]
                if path_len == 0 or path_len > 2000:
                    pos += 1
                    f.seek(pos)
                    continue

                path_data = f.read(path_len)
                pos += 4 + path_len
                if pos + 32 > file_base:
                    break

                # offset (8 bytes) + size (8 bytes) + md5 (16 bytes) = 32 bytes
                entry_data = f.read(32)
                if len(entry_data) < 32:
                    break
                file_offset = struct.unpack("<Q", entry_data[0:8])[0]
                file_size = struct.unpack("<Q", entry_data[8:16])[0]
                pos += 32

                try:
                    path_str = path_data.decode("utf-8").rstrip("\x00")
                    if path_str and file_offset > 0 and file_size > 0:
                        entries.append((path_str, file_offset, file_size))
                except UnicodeDecodeError:
                    pass

        return entries

    # ------------------------------------------------------------------
    # 文本提取
    # ------------------------------------------------------------------

    # .tscn/.tres 中常见的文本属性
    _TSCN_TEXT_PROPS = {
        "text", "dialog_text", "bbcode_text", "placeholder_text",
        "window_title", "hint_tooltip", "tooltip_text",
        "label_text", "button_text", "title", "description",
        "quest_name", "quest_description", "item_name", "item_description",
        "speaker_name", "dialogue", "line", "content",
    }

    def _extract_tscn(self, content: str, rel_path: str) -> list[TextItem]:
        """从 .tscn / .tres 文件中提取文本。"""
        items = []
        lines = content.split("\n")

        for i, line in enumerate(lines):
            stripped = line.strip()

            # 匹配属性赋值:  prop_name = "value"
            # Godot 也支持 prop_name = &'value' (StringName)
            m = re.match(r'(\w+)\s*=\s*"([^"]*)"', stripped)
            if not m:
                m = re.match(r"(\w+)\s*=\s*&'([^']*)'", stripped)
            if not m:
                continue

            prop_name = m.group(1).lower()
            value = m.group(2)

            # 检查属性名是否为文本类属性
            is_text_prop = prop_name in self._TSCN_TEXT_PROPS
            is_text_prop |= prop_name.endswith("_text") or prop_name.endswith("_name")
            is_text_prop |= "dialog" in prop_name or "text" in prop_name

            if is_text_prop and is_translatable(value):
                items.append(TextItem(
                    file=rel_path, key=f"line_{i}", original=value, line=i + 1,
                ))

        return items

    def _extract_gd(self, content: str, rel_path: str) -> list[TextItem]:
        """从 .gd GDScript 文件中提取文本。"""
        items = []

        # 匹配 tr("text") 调用
        for m in re.finditer(r'\btr\s*\(\s*"([^"]*)"\s*\)', content):
            text = m.group(1)
            if is_translatable(text):
                items.append(TextItem(
                    file=rel_path, key=f"tr_{m.start()}", original=text,
                ))

        # 匹配普通字符串赋值: var text = "hello"
        for m in re.finditer(
            r'(?:text|dialog(?:ue)?|message|line|content|name|title)\s*[=:]\s*"([^"]{2,})"',
            content, re.IGNORECASE,
        ):
            text = m.group(1)
            if is_translatable(text):
                items.append(TextItem(
                    file=rel_path, key=f"gd_{m.start()}", original=text,
                ))

        return items

    def _extract_csv(self, content: str, rel_path: str) -> list[TextItem]:
        """从 CSV 翻译文件中提取文本。"""
        items = []
        for i, line in enumerate(content.split("\n")):
            # 跳过表头和注释
            if i == 0 or line.startswith("#"):
                continue
            parts = self._split_csv_line(line)
            # Godot CSV 翻译格式: keys,en,ja,zh,...
            # 第2列通常是英语原文
            if len(parts) >= 2:
                key = parts[0].strip().strip('"')
                en_text = parts[1].strip().strip('"')
                if is_translatable(en_text) and len(en_text) > 1:
                    items.append(TextItem(
                        file=rel_path, key=key, original=en_text, line=i + 1,
                    ))
        return items

    def _extract_po(self, content: str, rel_path: str) -> list[TextItem]:
        """从 PO/translation 文件中提取文本。"""
        items = []
        current_msgid = ""
        in_msgid = False

        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("msgid "):
                m = re.match(r'msgid\s+"(.+)"', stripped)
                if m:
                    current_msgid = m.group(1)
                    in_msgid = True
            elif stripped.startswith('"') and in_msgid and not current_msgid:
                m = re.match(r'"(.+)"', stripped)
                if m:
                    current_msgid = m.group(1)
            elif stripped.startswith("msgstr "):
                if current_msgid and is_translatable(current_msgid):
                    items.append(TextItem(
                        file=rel_path, key=f"msgid_{len(items)}", original=current_msgid,
                    ))
                current_msgid = ""
                in_msgid = False

        return items

    def _extract_generic(self, content: str, rel_path: str) -> list[TextItem]:
        """通用文本行提取（含代码行过滤）。"""
        items = []
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            if len(stripped) <= 3:
                continue
            # Skip code-heavy lines: many {}();= but few letters
            code_chars = sum(1 for c in stripped if c in "{}();=")
            letters = sum(1 for c in stripped if c.isalpha())
            if code_chars > 0 and letters < 8 and code_chars >= letters * 0.5:
                continue
            if is_translatable(stripped):
                items.append(TextItem(
                    file=rel_path, key=f"line_{i}", original=stripped, line=i + 1,
                ))
        return items

    def _split_csv_line(self, line: str) -> list[str]:
        """简单的 CSV 行分割（处理引号包裹的字段）。"""
        parts = []
        current = ""
        in_quotes = False
        for ch in line:
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == ',' and not in_quotes:
                parts.append(current)
                current = ""
            else:
                current += ch
        parts.append(current)
        return parts


registry.register(GodotEngine())


def _count_files(path: Path) -> int:
    try:
        return sum(1 for p in path.rglob("*") if p.is_file())
    except Exception:
        return 0
