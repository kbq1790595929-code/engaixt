from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, debug, warning
from utils.text_extract import is_translatable


class RenPyEngine(EngineBase):
    name = "renpy"
    label = "Ren'Py 引擎"
    detect_priority = 95
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
            "处理 .rpy/.rpa 脚本文本、菜单和翻译字符串。",
            "保留 Ren'Py 变量、方括号表达式和脚本结构。",
        ),
    )

    _DIALOG_PATTERNS = [
        # say 语句: character "text"   (e.g. m "Hi, [player]!"  or  s 5c "text")
        re.compile(r'^\s*(\w+)(?:\s+\w+)?\s+"([^"]*)"\s*$'),
        # 直接字符串: "text"
        re.compile(r'^\s*"([^"]*)"\s*$'),
        # menu 选项: "Option":
        re.compile(r'^\s*"([^"]*)":\s*$'),
        # 翻译字符串 _("text")
        re.compile(r'_\(["\'](.+?)["\']\)'),
    ]

    def detect(self, path: Path) -> bool:
        if path.is_file():
            path = path.parent
        has_renpy_dir = (path / "renpy").is_dir() or (path / "game" / "renpy").is_dir()
        has_content = _has_renpy_content(path)
        return has_renpy_dir and has_content

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        evidence = []
        has_renpy_dir = (game_dir / "renpy").is_dir() or (game_dir / "game" / "renpy").is_dir()
        if has_renpy_dir:
            evidence.append("找到 renpy 运行时目录")
        counts = {
            ".rpy": int(_has_path_match(game_dir, "**/*.rpy")),
            ".rpyc": int(_has_path_match(game_dir, "**/*.rpyc")),
            ".rpa": int(_has_path_match(game_dir, "**/*.rpa")),
        }
        for ext, count in counts.items():
            if count:
                evidence.append(f"找到 {count} 个 {ext} 文件")
        if has_renpy_dir and any(counts.values()):
            return 97, evidence
        if any(counts.values()):
            return 60, evidence
        return 0, []

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        if (game_dir / "game").is_dir():
            game_dir = game_dir / "game"

        # Step 1: 解包 .rpa 文件（解到 original/ 以便后续回填复制）
        rpa_files = list(game_dir.glob("*.rpa"))
        if rpa_files:
            info(f"发现 {len(rpa_files)} 个 .rpa 文件，正在解包...")
            extract_dir = workspace / "original"
            extract_dir.mkdir(parents=True, exist_ok=True)
            for rpa in rpa_files:
                self._extract_rpa(rpa, extract_dir)
            source_dir = extract_dir
        else:
            # 没有 RPA，直接复制 game/ 下的文件到 original/
            source_dir = workspace / "original"
            source_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            for f in game_dir.glob("*"):
                dest = source_dir / f.name
                if f.is_file() and not dest.exists():
                    shutil.copy2(f, dest)

        # Step 2: 反编译 .rpyc 为 .rpy（优先使用反编译出的 .rpy）
        rpyc_files = list(source_dir.glob("*.rpyc"))
        if rpyc_files:
            info(f"发现 {len(rpyc_files)} 个 .rpyc 文件，正在反编译...")
            self._decompile_rpyc_batch(rpyc_files, source_dir)

        rpy_files = list(source_dir.glob("*.rpy"))
        if not rpy_files:
            info("反编译未生成 .rpy，回退到直接字符串提取...")
            return self._extract_strings_from_rpyc(source_dir)

        # Step 3: 解析 .rpy 文件，提取对话
        items: list[TextItem] = []
        for rpy_file in sorted(rpy_files):
            try:
                content = rpy_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            rel_path = str(rpy_file.relative_to(source_dir))
            lines = content.split("\n")

            for i, line in enumerate(lines):
                stripped = line.strip()
                for pattern in self._DIALOG_PATTERNS:
                    m = pattern.search(stripped)
                    if m:
                        groups = m.groups()
                        text = groups[-1]
                        if is_translatable(text):
                            items.append(TextItem(
                                file=rel_path,
                                key=f"line_{i}",
                                original=text,
                                line=i + 1,
                                meta={"raw_line": stripped, "source_dir": str(source_dir)},
                            ))
                        break

        info(f"提取到 {len(items)} 条可翻译文本（来自 {len(rpy_files)} 个文件）")
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        if not items:
            return

        # 按文件分组
        files: dict[str, list[TextItem]] = {}
        for item in items:
            if item.translated and item.translated != item.original:
                files.setdefault(item.file, []).append(item)

        if not files:
            info("没有需要回填的翻译")
            return

        source_dir = items[0].meta.get("source_dir") if items else None
        if not source_dir:
            source_dir = str(workspace / "original")

        source_path = Path(source_dir)

        for filename, file_items in files.items():
            target = source_path / filename
            if not target.exists():
                warning(f"找不到文件: {target}")
                continue

            content = target.read_text(encoding="utf-8", errors="replace")
            lines = content.split("\n")

            for item in file_items:
                if not (0 < item.line <= len(lines)):
                    continue

                orig_line = lines[item.line - 1]
                # 保留原始缩进
                indent = orig_line[:len(orig_line) - len(orig_line.lstrip())]
                stripped = orig_line.lstrip()

                # 在 stripped 行内替换原文为译文
                new_stripped = stripped.replace(item.original, item.translated, 1)
                lines[item.line - 1] = indent + new_stripped

            # 防线 3: UTF-8 无 BOM
            target.write_text("\n".join(lines), encoding="utf-8")
            info(f"回填: {filename} ({len(file_items)} 条)")

        info(f"回填完成: {len(files)} 个文件")
        info("提示: Ren'Py 会优先读取 .rpy 文件，翻译后的 .rpy 放回 game/ 目录即可生效")

    def _extract_rpa(self, rpa_path: Path, dest: Path):
        """解包 .rpa 文件。"""
        try:
            from unrpa import UnRPA
            info(f"  解包: {rpa_path.name} ...")
            unrpa = UnRPA(str(rpa_path), path=str(dest), mkdir=True, verbosity=0)
            unrpa.extract_files()
            info(f"  解包完成: {rpa_path.name}")
        except ImportError:
            warning("unrpa 未安装，请运行: pip install unrpa")
        except Exception as e:
            warning(f"解包 .rpa 失败: {e}")

    def _decompile_rpyc_batch(self, rpyc_files: list[Path], output_dir: Path):
        """使用 unrpyc CLI 批量反编译 .rpyc。"""
        unrpyc_path = self._find_unrpyc()
        if not unrpyc_path:
            warning("unrpyc 未找到，将使用内置字符串提取方案")
            return

        # unrpyc 接受文件名作为位置参数
        rpyc_names = [f.name for f in rpyc_files]
        cmd = ["python", str(unrpyc_path)] + rpyc_names

        try:
            result = subprocess.run(
                cmd,
                cwd=str(output_dir),
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                rpy_count = len(list(output_dir.glob("*.rpy")))
                info(f"反编译完成: {rpy_count} 个 .rpy 文件")
            else:
                # 如果不成功，尝试逐个处理
                info(f"批量反编译失败，尝试逐个处理...")
                for f in rpyc_files:
                    try:
                        subprocess.run(
                            ["python", str(unrpyc_path), f.name],
                            cwd=str(output_dir),
                            capture_output=True, text=True, timeout=30,
                        )
                    except Exception:
                        pass
                rpy_count = len(list(output_dir.glob("*.rpy")))
                if rpy_count:
                    info(f"逐个反编译完成: {rpy_count} 个 .rpy 文件")
        except subprocess.TimeoutExpired:
            warning("unrpyc 超时")
        except Exception as e:
            warning(f"反编译失败: {e}")

    def _find_unrpyc(self) -> Path | None:
        """查找 unrpyc.py 脚本路径。"""
        try:
            from core.tool_manager import ensure_tool
            managed = ensure_tool("unrpyc")
            if managed and managed.exists():
                return managed.resolve()
        except Exception:
            pass

        candidates = [
            Path(__file__).parent.parent / "unrpyc-master" / "unrpyc.py",
            Path("unrpyc-master/unrpyc.py"),
            Path("utils/unrpyc.py"),
            Path.home() / "Downloads" / ".game_translator" / "tools" / "unrpyc" / "unrpyc.py",
        ]
        for p in candidates:
            if p.exists():
                return p.resolve()
        return None

    def _extract_strings_from_rpyc(self, source_dir: Path) -> list[TextItem]:
        """兜底方案：从 .rpyc pickle 数据中直接提取字符串。"""
        import zlib

        items: list[TextItem] = []
        for rpyc_file in sorted(source_dir.glob("*.rpyc")):
            try:
                data = rpyc_file.read_bytes()

                # 查找 zlib 压缩数据
                for i in range(12, min(300, len(data))):
                    try:
                        decompressed = zlib.decompress(data[i:])
                        if len(decompressed) < 500:
                            continue

                        # 从 pickle 字节流中提取可读字符串
                        text_segments = self._extract_pickle_strings(decompressed)
                        for seg in text_segments:
                            if is_translatable(seg):
                                items.append(TextItem(
                                    file=rpyc_file.name,
                                    original=seg,
                                ))
                        break
                    except Exception:
                        continue
            except Exception as e:
                debug(f"处理 {rpyc_file.name} 失败: {e}")

        info(f"从 .rpyc 直接提取到 {len(items)} 条文本")
        return items

    def _extract_pickle_strings(self, data: bytes) -> list[str]:
        """从 Ren'Py pickle/序列化数据中提取可读字符串。"""
        results = []
        i = 0
        current = bytearray()
        in_string = False

        while i < len(data):
            b = data[i]
            if 32 <= b < 127:
                current.append(b)
            else:
                if len(current) >= 4:
                    try:
                        s = current.decode("ascii")
                        # 过滤掉明显不是对话的字符串
                        if (
                            not s.startswith("c") and " " in s
                            and not s.startswith("(") and not s.startswith(".")
                        ):
                            results.append(s)
                        elif any(c.isalpha() for c in s) and len(s) >= 8:
                            # 可能是角色名或短句
                            if not s[0].isupper() or len(s) < 20:
                                pass  # skip single words
                            else:
                                results.append(s)
                    except UnicodeDecodeError:
                        pass
                current = bytearray()
            i += 1

        return results

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        from core.exe_selector import find_main_exe

        exe = find_main_exe(game_dir, recursive=False)
        if exe:
            return exe
        exe_patterns = ["*.exe", "*.sh", "*.app"]
        for pattern in exe_patterns:
            candidates = list(game_dir.glob(pattern))
            if candidates:
                # 优先选与目录名匹配的 exe
                dir_name = game_dir.name.lower().replace(" ", "")
                for c in candidates:
                    if c.stem.lower().replace(" ", "") in dir_name:
                        return c
                return candidates[0]
        parent_exes = list(game_dir.parent.glob("*.exe"))
        return parent_exes[0] if parent_exes else None


def _has_path_match(path: Path, pattern: str) -> bool:
    try:
        return next(path.glob(pattern), None) is not None
    except OSError:
        return False


def _has_renpy_content(path: Path) -> bool:
    for pattern in ("game/*.rpy", "game/*.rpyc", "game/*.rpa", "game/*.rpymc"):
        if _has_path_match(path, pattern):
            return True
    for pattern in ("**/*.rpy", "**/*.rpyc", "**/*.rpa", "**/*.rpymc"):
        if _has_path_match(path, pattern):
            return True
    return False


registry.register(RenPyEngine())
