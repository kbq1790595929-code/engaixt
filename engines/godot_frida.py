"""Godot Frida 引擎 — 加密 PCK 的密钥捕获 + 解密 + 翻译 + 回填全流程"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import subprocess
import time
from pathlib import Path

from core.exe_selector import find_main_exe
from core.resources import resource_path
from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, warning, debug
from utils.text_extract import is_translatable


# ---------------------------------------------------------------------------
# 文本提取（从 GodotEngine 移植，保持一致性）
# ---------------------------------------------------------------------------

_TSCN_TEXT_PROPS = {
    "text", "dialog_text", "bbcode_text", "placeholder_text",
    "window_title", "hint_tooltip", "tooltip_text",
    "label_text", "button_text", "title", "description",
    "quest_name", "quest_description", "item_name", "item_description",
    "speaker_name", "dialogue", "line", "content",
}

_SKIP_DIRS = {".godot", ".import", "__pycache__", ".git", "fonts", "shaders",
              "assets", "images", "audio", "music", "sounds", "sfx", "bgm"}


def _extract_tscn(content: str, rel_path: str) -> list[TextItem]:
    items = []
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        m = re.match(r'(\w+)\s*=\s*"([^"]*)"', stripped)
        if not m:
            m = re.match(r"(\w+)\s*=\s*&'([^']*)'", stripped)
        if not m:
            continue
        prop_name = m.group(1).lower()
        value = m.group(2)
        is_text = prop_name in _TSCN_TEXT_PROPS
        is_text |= prop_name.endswith("_text") or prop_name.endswith("_name")
        is_text |= "dialog" in prop_name or "text" in prop_name
        if is_text and is_translatable(value):
            items.append(TextItem(file=rel_path, key=f"line_{i}", original=value, line=i + 1))
    return items


def _extract_gd(content: str, rel_path: str) -> list[TextItem]:
    items = []
    for m in re.finditer(r'\btr\s*\(\s*"([^"]*)"\s*\)', content):
        text = m.group(1)
        if is_translatable(text):
            items.append(TextItem(file=rel_path, key=f"tr_{m.start()}", original=text))
    for m in re.finditer(
        r'(?:text|dialog(?:ue)?|message|line|content|name|title)\s*[=:]\s*"([^"]{2,})"',
        content, re.IGNORECASE,
    ):
        text = m.group(1)
        if is_translatable(text):
            items.append(TextItem(file=rel_path, key=f"gd_{m.start()}", original=text))
    return items


def _extract_csv(content: str, rel_path: str) -> list[TextItem]:
    items = []
    for i, line in enumerate(content.split("\n")):
        if i == 0 or line.startswith("#"):
            continue
        parts = _split_csv(line)
        if len(parts) >= 2:
            key = parts[0].strip().strip('"')
            en = parts[1].strip().strip('"')
            if is_translatable(en) and len(en) > 1:
                items.append(TextItem(file=rel_path, key=key, original=en, line=i + 1))
    return items


def _extract_po(content: str, rel_path: str) -> list[TextItem]:
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
                items.append(TextItem(file=rel_path, key=f"msgid_{len(items)}", original=current_msgid))
            current_msgid = ""
            in_msgid = False
    return items


def _extract_generic(content: str, rel_path: str) -> list[TextItem]:
    items = []
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        if len(stripped) > 3 and is_translatable(stripped):
            items.append(TextItem(file=rel_path, key=f"line_{i}", original=stripped, line=i + 1))
    return items


def _split_csv(line: str) -> list[str]:
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


# ---------------------------------------------------------------------------
# 幂等性缓存
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / "Downloads" / ".game_translator" / "frida_cache"


def _compute_exe_md5(exe_path: Path) -> str:
    h = hashlib.md5()
    with open(exe_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_cache(exe_md5: str) -> dict | None:
    cache_file = _CACHE_DIR / f"{exe_md5}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _save_cache(exe_md5: str, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _CACHE_DIR / f"{exe_md5}.json"
    cache_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# GodotFridaEngine
# ---------------------------------------------------------------------------

class GodotFridaEngine(EngineBase):
    name = "godot_frida"
    label = "Godot 引擎 (加密 PCK - Frida)"
    support_level = "beta"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=False,
        runtime_patch=True,
        creates_launcher=True,
        portable_after_patch=False,
        requires_python=True,
        requires_frida=True,
        notes=(
            "用于加密 PCK 或静态解包不可行的 Godot 游戏。",
            "可能结合运行时 hook 与 Godot PCK 静态补丁逻辑。",
        ),
    )
    detect_priority = 96

    # 需要跳过的非文本扩展
    _SKIP_EXTS = {".png", ".jpg", ".jpeg", ".ogg", ".mp3", ".wav", ".webp", ".svg",
                  ".ttf", ".otf", ".woff", ".woff2", ".ico", ".icns",
                  ".import", ".sample", ".scn"}

    _TEXT_EXTS = {".tscn", ".gd", ".tres", ".tscn.remap", ".tres.remap",
                  ".txt", ".json", ".cfg", ".csv", ".po", ".translation", ".res", ".dtl"}

    def __init__(self):
        super().__init__()
        self._captured_key: str = ""

    def detect(self, path: Path) -> bool:
        """检测是否为加密 Godot PCK 游戏。只匹配加密 PCK，未加密的留给 godot_pck。"""
        if path.is_file():
            p = path.parent
        else:
            p = path

        pck_files = list(p.glob("*.pck"))
        if not pck_files:
            for sub in ["contents", "game", "data"]:
                d = p / sub
                if d.is_dir():
                    pck_files = list(d.glob("*.pck"))
                    if pck_files:
                        break

        if not pck_files:
            return False

        pck_path = pck_files[0]
        if not self._is_valid_pck(pck_path):
            return False

        return self._is_pck_encrypted(pck_path)

    def _is_valid_pck(self, pck_path: Path) -> bool:
        """检查是否为有效的 Godot PCK 文件。"""
        try:
            with open(pck_path, "rb") as f:
                header = f.read(48)
                return len(header) >= 48 and header[:4] == b"GDPC"
        except Exception:
            return False

    def _is_pck_encrypted(self, pck_path: Path) -> bool:
        """检测 PCK 是否启用加密。优先检查 PCK header flags，再检查数据特征。"""
        try:
            with open(pck_path, "rb") as f:
                header = f.read(48)
                if len(header) < 48 or header[:4] != b"GDPC":
                    return False

                # Godot 4 PCK flags at offset 20: bit 0 = encrypted
                flags = struct.unpack_from("<I", header, 20)[0]
                if flags & 1:
                    return True

                # flags=0 但文件数据可能通过脚本加密单独加密
                # 检查 .gdc 文件是否有 Godot 加密头 GDEC
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
                    entry_data = f.read(32)
                    entry_offset = struct.unpack("<Q", entry_data[0:8])[0]
                    entry_size = struct.unpack("<Q", entry_data[8:16])[0]
                    pos += 32
                    try:
                        inner_name = path_data.decode("utf-8").rstrip("\x00")
                    except UnicodeDecodeError:
                        continue

                    if inner_name.endswith(".gdc") and entry_size > 4:
                        f.seek(entry_offset)
                        magic = f.read(4)
                        if magic == b"GDEC":
                            return True
                        break
        except Exception:
            pass
        return False

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        """解包流程：
        - 加密 PCK：挂起启动 → Frida 捕获密钥 → 解密 PCK → 提取文本
        - 未加密 PCK：gdre_tools 恢复项目 → GodotEngine 提取文本
        """
        # 1. 定位 EXE 和 PCK
        game_exe, pck_path = self._find_exe_and_pck(path)
        info(f"目标: {game_exe}")
        info(f"PCK: {pck_path}")

        # 2. 判断是否加密
        encrypted = self._is_pck_encrypted(pck_path)

        if encrypted:
            info("检测到加密 PCK，启动 Frida 密钥捕获流程...")
            items = self._unpack_encrypted(game_exe, pck_path, workspace)
        else:
            info("PCK 未加密，使用 gdre_tools 恢复项目...")
            items = self._unpack_unencrypted(pck_path, workspace)

        # 嵌入 PCK 路径供 repack 使用
        for item in items:
            item.meta["pck_path"] = str(pck_path)
            item.meta["pck_exe"] = str(game_exe)

        return items

    def _unpack_encrypted(self, game_exe: Path, pck_path: Path, workspace: Path) -> list[TextItem]:
        """处理加密 PCK：Frida 捕获密钥 → 解密 → 提取文本。"""
        from config import get_config
        from core.frida_injector import launch_suspended, inject_and_capture_key, terminate_process

        config = get_config()

        exe_md5 = _compute_exe_md5(game_exe)
        cache = _load_cache(exe_md5)
        if cache and cache.get("key"):
            info("幂等性缓存命中: 复用已捕获的密钥")
            self._captured_key = cache["key"]
            key = self._captured_key
        else:
            probe_js = resource_path("frida_probe.js")
            timeout = config.frida_timeout

            info("启动游戏（挂起模式），注入 Frida 探针...")
            proc_info = launch_suspended(game_exe)

            key = None
            frida_error = None
            try:
                key = inject_and_capture_key(proc_info, probe_js, timeout=timeout)
            except Exception as e:
                frida_error = str(e)
                try:
                    terminate_process(proc_info)
                except Exception:
                    pass

            if not key:
                if frida_error:
                    warning(f"Frida 注入失败: {frida_error}")
                info("降级到静态 EXE 密钥扫描...")
                key = self._extract_key_static(game_exe, pck_path)

            if not key:
                raise RuntimeError(
                    f"未能捕获 AES 密钥（Frida + 静态扫描均失败）。\n"
                    f"Frida 错误: {frida_error or '超时无密钥'}"
                )

            self._captured_key = key
            _save_cache(exe_md5, {
                "key": key, "exe_md5": exe_md5,
                "exe_path": str(game_exe), "pck_path": str(pck_path),
                "timestamp": time.time(),
            })
            info("密钥已缓存")

        decrypted_dir = workspace / "decrypted"
        decrypted_dir.mkdir(parents=True, exist_ok=True)
        info(f"解密 PCK 到: {decrypted_dir}")
        self._decrypt_pck(pck_path, key, decrypted_dir)

        items = self._extract_texts(decrypted_dir, workspace)
        info(f"从解密文件提取到 {len(items)} 条可翻译文本")

        for item in items:
            item.meta["pck_key"] = key

        return items

    def _unpack_unencrypted(self, pck_path: Path, workspace: Path) -> list[TextItem]:
        """处理未加密 PCK：gdre_tools 恢复项目 → 直接提取文本。"""
        # 1. 用 gdre_tools 恢复项目
        recovered_dir = workspace / "recovered"
        self._recover_with_gdre_tools(pck_path, recovered_dir)

        # 2. 确保有 project.godot（标识恢复的项目）
        project_file = recovered_dir / "project.godot"
        if not project_file.exists():
            project_file.write_text("""[application]
config/name="Recovered Game"
config/features=PackedStringArray("4.4")
""", encoding="utf-8")

        # 3. 直接从恢复目录提取文本（复用已有提取逻辑）
        items = self._extract_texts(recovered_dir, workspace)
        info(f"从未加密 PCK 提取到 {len(items)} 条可翻译文本")
        return items

    def _recover_with_gdre_tools(self, pck_path: Path, output_dir: Path):
        """使用 gdre_tools 从 PCK 恢复完整 Godot 项目。"""
        info(f"gdre_tools 恢复: {pck_path} → {output_dir}")

        gdre_exe = self._find_gdre_tools()
        if not gdre_exe:
            raise RuntimeError(
                "未找到 gdre_tools。请安装 Godot RE Tools:\n"
                "  winget install --id=GDRETools.gdsdecomp -e\n"
                "或下载: https://github.com/GDRETools/gdsdecomp/releases"
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(gdre_exe), "--headless",
            f"--recover={pck_path}",
            f"--output={output_dir}",
        ]

        info(f"运行: {' '.join(cmd)}")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            # gdre_tools 有时 returncode 不为 0 但仍然成功恢复
            if output_dir.is_dir() and any(output_dir.iterdir()):
                warning(f"gdre_tools 退出码: {result.returncode}，但文件已生成，继续...")
            else:
                raise RuntimeError(f"gdre_tools 恢复失败:\n{stderr}")

        # 统计结果
        file_count = sum(1 for _ in output_dir.rglob("*") if _.is_file())
        info(f"gdre_tools 恢复完成: {file_count} 个文件")

    def _find_gdre_tools(self) -> Path | None:
        """查找 gdre_tools.exe 的安装路径。"""
        # 1. 检查 winget 安装路径
        winget_base = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
        if winget_base.is_dir():
            for d in winget_base.iterdir():
                if d.is_dir() and "gdsdecomp" in d.name.lower():
                    exe = d / "gdre_tools.exe"
                    if exe.exists():
                        return exe

        # 2. 检查 tools 目录
        from core.tool_manager import get_tools_dir
        tools_exe = get_tools_dir() / "gdre_tools.exe"
        if tools_exe.exists():
            return tools_exe

        # 3. 检查 PATH
        import shutil
        which = shutil.which("gdre_tools")
        if which:
            return Path(which)

        return None

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        """回填翻译 → 放到 workspace/original/ 供 pipeline 复制。"""
        if not items:
            return

        translated = [it for it in items if it.translated and it.translated != it.original]
        if not translated:
            info("没有需要回填的翻译")
            return

        recovered_dir = workspace / "recovered"
        decrypted_dir = workspace / "decrypted"

        if recovered_dir.is_dir():
            self._repack_unencrypted(translated, recovered_dir, workspace)
        elif decrypted_dir.is_dir():
            self._repack_encrypted(translated, decrypted_dir, workspace)
        else:
            warning("未找到 recovered 或 decrypted 目录，无法回填")
            return

    def _repack_unencrypted(self, translated: list[TextItem], recovered_dir: Path, workspace: Path):
        """回填翻译到恢复的项目文件，复制到 original/ 供 pipeline 复制到游戏目录。"""
        # 1. 写回翻译到恢复文件
        files: dict[str, list[TextItem]] = {}
        for item in translated:
            files.setdefault(item.file, []).append(item)

        modified: set[str] = set()
        for filename, file_items in files.items():
            target = recovered_dir / filename
            if not target.exists():
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            for item in file_items:
                if item.original in content:
                    content = content.replace(item.original, item.translated, 1)
            target.write_text(content, encoding="utf-8")
            modified.add(filename)
            info(f"回填译文: {filename} ({len(file_items)} 条)")

        # 2. 尝试 txt→bin 转换 + PCK 打包
        gdre_exe = self._find_gdre_tools()
        original_dir = workspace / "original"
        original_dir.mkdir(parents=True, exist_ok=True)

        pck_built = False
        if gdre_exe:
            pck_built = self._try_build_pck(gdre_exe, recovered_dir, modified,
                                            translated, original_dir, workspace)

        # 3. 降级：复制修改后的文本文件到 original/ 作为散文件
        if not pck_built:
            info("使用散文件回填方式（游戏目录下放置翻译后的文本文件）")
            for filename in modified:
                src = recovered_dir / filename
                dst = original_dir / filename
                if src.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

        info(f"回填完成: {len(modified)} 个文件, {len(translated)} 条翻译")

    def _try_build_pck(self, gdre_exe: Path, recovered_dir: Path, modified: set[str],
                       translated: list[TextItem], original_dir: Path, workspace: Path) -> bool:
        """尝试用 gdre_tools 将翻译后的文件重新打包为 PCK。成功返回 True。"""
        import shutil

        # Step A: 将 .tscn/.tres 转换为二进制 .scn/.res
        converted: dict[str, Path] = {}
        for filename in modified:
            src = recovered_dir / filename
            if not src.exists():
                continue
            ext = src.suffix.lower()
            if ext not in (".tscn", ".tres"):
                continue
            bin_ext = ".scn" if ext == ".tscn" else ".res"
            bin_file = src.with_suffix(bin_ext)
            # 尝试 txt→bin 转换
            result = subprocess.run(
                [str(gdre_exe), "--headless", f"--txt-to-bin={src}"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and bin_file.exists():
                converted[filename] = bin_file
                info(f"二进制转换: {filename} → {bin_file.name}")

        if not converted:
            return False

        # Step B: 构建临时目录，用二进制文件替换文本文件
        tmp_dir = workspace / "pck_build"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        shutil.copytree(recovered_dir, tmp_dir)

        for rel_path, bin_file in converted.items():
            bin_rel = Path(rel_path).with_suffix(bin_file.suffix)
            dst = tmp_dir / bin_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(bin_file.read_bytes())
            text_dst = tmp_dir / rel_path
            if text_dst.exists():
                text_dst.unlink()

        # Step C: 创建新 PCK
        pck_path_str = translated[0].meta.get("pck_path", "")
        pck_name = Path(pck_path_str).name if pck_path_str else "game.pck"
        output_pck = original_dir / pck_name

        result = subprocess.run(
            [str(gdre_exe), "--headless",
             f"--pck-create={tmp_dir}",
             "--pck-version=2",
             f"--output={output_pck}"],
            capture_output=True, text=True, timeout=120,
        )

        shutil.rmtree(tmp_dir, ignore_errors=True)

        if result.returncode == 0 and output_pck.exists():
            info(f"PCK 打包完成: {output_pck} ({output_pck.stat().st_size} bytes)")
            # 备份原始 PCK
            bak = original_dir / (pck_name + ".bak")
            orig_pck = Path(pck_path_str) if pck_path_str else None
            if orig_pck and orig_pck.exists() and not bak.exists():
                bak.write_bytes(orig_pck.read_bytes())
            return True

        return False

    def _repack_encrypted(self, translated: list[TextItem], decrypted_dir: Path, workspace: Path):
        """回填翻译到解密文件 → 重建加密 PCK（含字体替换）。"""
        from engines.godot_pck import patch_dtl_content, patch_inline_strings_safe, rebuild_pck

        key = self._captured_key or translated[0].meta.get("pck_key", "")
        if not key:
            warning("未找到加密密钥，无法重新加密 PCK")
            return

        pck_path_str = translated[0].meta.get("pck_path", "")
        orig_pck = Path(pck_path_str) if pck_path_str else None
        if not orig_pck or not orig_pck.exists():
            warning("未找到原始 PCK 文件")
            return

        info("重建加密 PCK（含 DTL/SCN 回填 + 字体替换）...")

        # Group translations by file
        dtl_trans: dict[str, dict[str, str]] = {}
        scn_trans: dict[str, dict[str, str]] = {}

        for item in translated:
            ext = Path(item.file).suffix.lower()
            if ext == ".dtl":
                dtl_trans.setdefault(item.file, {})[item.original] = item.translated
            elif ext in (".scn", ".res"):
                scn_trans.setdefault(item.file, {})[item.original] = item.translated

        # Build patched map: {pck_path: encrypted_data}
        all_patched: dict[str, bytes] = {}
        _norm = lambda p: p.replace("\\", "/")

        # Patch DTL files
        dtl_count = 0
        for rel_path, trans_map in dtl_trans.items():
            target = decrypted_dir / rel_path
            if not target.exists():
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            patched, n = patch_dtl_content(content, trans_map)
            if n > 0:
                all_patched[_norm(rel_path)] = patched.encode("utf-8")
                dtl_count += n
        info(f"DTL: {dtl_count} 处替换 ({len([k for k in all_patched if k.endswith('.dtl')])} 个文件)")

        # Patch SCN/RES files (variable-length inline)
        scn_shorter = 0
        scn_longer = 0
        scn_equal = 0
        for rel_path, trans_map in scn_trans.items():
            target = decrypted_dir / rel_path
            if not target.exists():
                continue
            data = target.read_bytes()
            patched, r_short, r_long, r_eq = patch_inline_strings_safe(data, trans_map)
            if r_short + r_long + r_eq > 0:
                all_patched[_norm(rel_path)] = patched
                scn_shorter += r_short
                scn_longer += r_long
                scn_equal += r_eq
        info(f"SCN/RES: {scn_shorter} 缩短, {scn_longer} 插入, {scn_equal} 等长")

        # Font replacement
        from config import get_config
        config = get_config()
        cjk_font = config.cjk_font_path
        if cjk_font:
            cjk_ttf = Path(cjk_font)
            if cjk_ttf.exists():
                imported_dir = decrypted_dir / ".godot" / "imported"
                if imported_dir.is_dir():
                    font_patches = self._build_font_patches_encrypted(
                        cjk_ttf, imported_dir, decrypted_dir)
                    all_patched.update(font_patches)
                    info(f"字体: {len(font_patches)} 个替换")

        if not all_patched:
            info("没有需要修改的内容")
            return

        # Rebuild PCK
        output_pck = workspace / "original" / orig_pck.name
        output_pck.parent.mkdir(parents=True, exist_ok=True)
        info(f"重建 PCK: {len(all_patched)} 个文件已修改")
        rebuild_pck(orig_pck, all_patched, output_pck, decrypted_dir=decrypted_dir)
        info(f"PCK 已重建: {output_pck.name} ({output_pck.stat().st_size:,} bytes)")

    def _build_font_patches_encrypted(self, cjk_ttf: Path, imported_dir: Path,
                                      decrypted_dir: Path) -> dict[str, bytes]:
        """Build font replacement patches for encrypted PCK."""
        from core.font_replacer import get_bundled_fontdata

        cjk_data = get_bundled_fontdata()
        if not cjk_data:
            warning("未找到内嵌 CJK 字体资源，跳过字体替换")
            return {}

        patches = {}
        for fd in imported_dir.glob("*.fontdata"):
            try:
                magic = fd.read_bytes()[:4]
            except Exception:
                continue
            if magic == b"RSCC":
                rel = str(fd.relative_to(decrypted_dir)).replace("\\", "/")
                patches[rel] = cjk_data
                info(f"  字体: {fd.name} → CJK")
        return patches

    def find_exe(self, path: Path) -> Path | None:
        game_exe, _ = self._find_exe_and_pck(path)
        return game_exe

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _find_exe_and_pck(self, path: Path) -> tuple[Path, Path]:
        """定位游戏 EXE 和 PCK 文件。"""
        if path.is_file():
            base = path.parent
        else:
            base = path

        # 找 EXE
        exe_path: Path | None = find_main_exe(base, recursive=False)
        if not exe_path:
            # 检查上级目录
            parent = base.parent
            if parent != base:
                exe_path = find_main_exe(parent, recursive=False)
                if exe_path:
                    base = parent
        if not exe_path:
            raise FileNotFoundError(f"未找到游戏 EXE: {base}")

        # 找 PCK
        pck_path: Path | None = None
        for d in [base] + [base / s for s in ["contents", "game", "data"]]:
            if d.is_dir():
                pcks = list(d.glob("*.pck"))
                if pcks:
                    # 取最大的 PCK
                    pck_path = max(pcks, key=lambda x: x.stat().st_size)
                    break
        if not pck_path:
            raise FileNotFoundError(f"未找到 PCK 文件: {base}")

        return exe_path, pck_path

    def _extract_key_static(self, game_exe: Path, pck_path: Path) -> str | None:
        """静态扫描 EXE 数据段中的 64 字符 hex 密钥，并通过试解密 PCK 验证。

        Godot 4 将 AES-256 密钥以 hex 字符串形式存储在 .rdata 段中。
        此方法作为 Frida 注入失败时的降级方案。
        """
        info("静态扫描 EXE 数据段中的候选密钥...")
        exe_bytes = game_exe.read_bytes()

        # 解析 PE 获取数据段范围
        pe_offset = struct.unpack_from("<I", exe_bytes, 0x3C)[0]
        num_sections = struct.unpack_from("<H", exe_bytes, pe_offset + 6)[0]
        opt_header_size = struct.unpack_from("<H", exe_bytes, pe_offset + 20)[0]
        section_start = pe_offset + 24 + opt_header_size

        data_ranges: list[tuple[int, int]] = []
        for i in range(num_sections):
            off = section_start + i * 40
            name = exe_bytes[off:off + 8].rstrip(b"\x00").decode("ascii", errors="replace")
            raw_ptr = struct.unpack_from("<I", exe_bytes, off + 20)[0]
            raw_size = struct.unpack_from("<I", exe_bytes, off + 16)[0]
            virt_size = struct.unpack_from("<I", exe_bytes, off + 8)[0]
            section_size = min(raw_size, virt_size) if virt_size > 0 else raw_size
            if name in (".rdata", ".data"):
                data_ranges.append((raw_ptr, min(section_size, len(exe_bytes) - raw_ptr)))
                info(f"  数据段 {name}: offset=0x{raw_ptr:X}, size=0x{section_size:X}")

        if not data_ranges:
            warning("未找到 .rdata 或 .data 段，扫描整个 EXE（较慢）...")
            data_ranges.append((0, len(exe_bytes)))

        # 用正则搜索 64 字符 hex 字符串
        hex_pattern = re.compile(rb"[0-9a-fA-F]{64}")
        candidates: dict[str, int] = {}  # hex -> first offset
        seen = set()

        for raw_ptr, size in data_ranges:
            chunk = exe_bytes[raw_ptr:raw_ptr + size]
            for m in hex_pattern.finditer(chunk):
                hex_key = m.group().decode("ascii").upper()
                if hex_key in seen:
                    continue
                seen.add(hex_key)

                # 熵过滤：跳过低熵/测试密钥
                key_bytes = bytes.fromhex(hex_key)
                unique = len(set(key_bytes))
                if unique < 10:
                    continue
                # 跳过重复模式（如 1234567890123456...）
                if unique < 20 and len(set(key_bytes[:8])) <= 4:
                    continue

                candidates[hex_key] = raw_ptr + m.start()

        info(f"找到 {len(candidates)} 个 64 字符 hex 候选密钥")

        if not candidates:
            return None

        # 用每个候选尝试解密 PCK 中的第一个文本文件来验证
        try:
            from Crypto.Cipher import AES
        except ImportError:
            warning("未安装 pycryptodome，无法验证候选密钥")
            return None

        # 从 PCK 取一个测试文件（优先小文本文件）
        test_entry = self._get_test_entry(pck_path)
        if not test_entry:
            warning("PCK 中没有合适的测试文件")
            return None

        test_name, test_off, test_size = test_entry
        debug(f"测试文件: {test_name} (offset={test_off}, size={test_size})")

        with open(pck_path, "rb") as f:
            f.seek(test_off)
            encrypted = f.read(test_size)

        for hex_key, exe_offset in candidates.items():
            try:
                key_bytes = bytes.fromhex(hex_key)
                cipher = AES.new(key_bytes, AES.MODE_ECB)
                aligned = encrypted[:((len(encrypted) // 16) * 16)]
                decrypted = cipher.decrypt(aligned)
                decrypted = _unpad_pkcs7(decrypted)

                # 验证：检查明文是否像有效数据
                if decrypted[:4] == b"GDPC":
                    continue  # 解密出另一个 PCK 头

                ascii_count = sum(32 <= b <= 126 for b in decrypted[:200])
                if ascii_count > 80:
                    info(f"静态扫描验证成功! 密钥: {hex_key} (EXE偏移: 0x{exe_offset:X})")
                    return hex_key
            except Exception:
                continue

        warning(f"所有 {len(candidates)} 个候选密钥均未通过验证")
        return None

    def _get_test_entry(self, pck_path: Path) -> tuple[str, int, int] | None:
        """从 PCK 中找一个小的文本文件用于测试解密。"""
        try:
            with open(pck_path, "rb") as f:
                header = f.read(48)
                if header[:4] != b"GDPC":
                    return None
                file_base = struct.unpack_from("<Q", header, 24)[0]

                entries = []
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
                    entry_data = f.read(32)
                    entry_offset = struct.unpack("<Q", entry_data[0:8])[0]
                    entry_size = struct.unpack("<Q", entry_data[8:16])[0]
                    pos += 32
                    try:
                        name = path_data.decode("utf-8").rstrip("\x00")
                        entries.append((name, entry_offset, entry_size))
                    except UnicodeDecodeError:
                        pass

            # 优先文本文件
            for name, off, sz in entries:
                if sz > 16 and sz < 50000 and name.endswith((".json", ".tscn", ".gd", ".csv", ".txt")):
                    return (name, off, sz)
            # 降级到任意小文件
            for name, off, sz in entries:
                if sz > 16 and sz < 50000:
                    return (name, off, sz)
            # 最后任意文件
            for name, off, sz in entries:
                if sz > 16 and sz < 200000:
                    return (name, off, sz)
        except Exception:
            pass
        return None

    def _decrypt_pck(self, pck_path: Path, key: str, output_dir: Path):
        """解密 PCK 文件。优先用 gdpack，降级到内置 AES-ECB。"""
        from core.tool_manager import ensure_tool

        # 尝试 gdpack
        gdpack = ensure_tool("gdpack")
        if gdpack:
            result = subprocess.run(
                [str(gdpack), "--decrypt", str(pck_path), "--key", key, "-o", str(output_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                info("gdpack 解密成功")
                return
            debug(f"gdpack 解密失败: {result.stderr}")

        # 尝试 gdsdecomp
        gdsdecomp = ensure_tool("gdsdecomp")
        if gdsdecomp:
            result = subprocess.run(
                [str(gdsdecomp), str(pck_path), "--key", key, "-o", str(output_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                info("gdsdecomp 解密成功")
                return
            debug(f"gdsdecomp 解密失败: {result.stderr}")

        # 降级：内置 AES-256-ECB 逐文件解密
        info("使用内置 AES-256-ECB 解密 PCK...")
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise RuntimeError(
                "需要 pycryptodome 库进行内置解密。\n"
                "请运行: pip install pycryptodome"
            )

        key_bytes = bytes.fromhex(key)
        if len(key_bytes) != 32:
            raise ValueError(f"密钥长度错误: {len(key_bytes)} 字节（需要 32 字节）")

        cipher = AES.new(key_bytes, AES.MODE_ECB)

        with open(pck_path, "rb") as f:
            header = f.read(48)
            if header[:4] != b"GDPC":
                raise ValueError("不是有效的 Godot PCK 文件")

            file_base = struct.unpack_from("<Q", header, 24)[0]

            # 已解密的文件跟踪
            written_files: set[str] = set()

            def _decrypt_file(file_path: str, offset: int, size: int):
                if file_path in written_files:
                    return
                written_files.add(file_path)

                ext = Path(file_path).suffix.lower()
                if ext in self._SKIP_EXTS or size > 10 * 1024 * 1024:
                    return

                f.seek(offset)
                encrypted = f.read(size)
                # AES-ECB 需要填充到 16 字节对齐
                if len(encrypted) % 16 != 0:
                    # Godot 可能不填充整个文件，只加密主要部分
                    # 尝试解密对齐的部分
                    aligned_len = (len(encrypted) // 16) * 16
                    if aligned_len == 0:
                        # 文件太小无法对齐，写入原文
                        dest_file = output_dir / file_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        dest_file.write_bytes(encrypted)
                        return
                    to_decrypt = encrypted[:aligned_len]
                    rest = encrypted[aligned_len:]
                    try:
                        decrypted = cipher.decrypt(to_decrypt)
                        # PKCS7 去填充
                        decrypted = _unpad_pkcs7(decrypted)
                    except Exception:
                        decrypted = to_decrypt  # 解密失败，保留原文
                    dest_file = output_dir / file_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    dest_file.write_bytes(decrypted + rest)
                else:
                    try:
                        decrypted = cipher.decrypt(encrypted)
                        decrypted = _unpad_pkcs7(decrypted)
                    except Exception:
                        decrypted = encrypted
                    dest_file = output_dir / file_path
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    dest_file.write_bytes(decrypted)

            # 解析 PCK 索引
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
                entry_data = f.read(32)
                file_offset = struct.unpack("<Q", entry_data[0:8])[0]
                file_size = struct.unpack("<Q", entry_data[8:16])[0]
                pos += 32

                try:
                    inner_path = path_data.decode("utf-8").rstrip("\x00")
                    if inner_path and file_offset > 0 and file_size > 0:
                        _decrypt_file(inner_path, file_offset, file_size)
                except UnicodeDecodeError:
                    pass

        info(f"内置解密完成: {len(written_files)} 个文件")

    def _encrypt_pck(self, source_dir: Path, key: str, output_pck: Path):
        """重新加密 PCK。优先用 gdpack，降级到内置 AES-ECB。"""
        from core.tool_manager import ensure_tool

        gdpack = ensure_tool("gdpack")
        if gdpack:
            result = subprocess.run(
                [str(gdpack), "--encrypt", str(source_dir), "--key", key, "-o", str(output_pck)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                info("gdpack 加密成功")
                return

        # 降级：查找原始 PCK 并原地替换加密数据
        # 需要原始 PCK 路径（从缓存获取）
        info("使用内置 AES-ECB 加密...")
        try:
            from Crypto.Cipher import AES
        except ImportError:
            raise RuntimeError("需要 pycryptodome 库")

        raise RuntimeError(
            "需要 gdpack 工具进行重新加密。\n"
            "请将 gdpack.exe 放到工具目录中，或在设置中启用自动下载。\n"
            "工具目录: ~/Downloads/.game_translator/tools/"
        )

    def _extract_texts(self, root_dir: Path, workspace: Path) -> list[TextItem]:
        """从解密目录提取可翻译文本。"""
        items: list[TextItem] = []

        for dirpath, dirnames, filenames in root_dir.walk():
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in self._TEXT_EXTS:
                    continue

                filepath = dirpath / fname
                try:
                    size = filepath.stat().st_size
                except OSError:
                    continue
                if size < 10 or size > 5 * 1024 * 1024:
                    continue

                rel = str(filepath.relative_to(root_dir))

                # 二进制 .res/.scn 文件 → 用统一 VARIANT_STRING (type=5) 提取
                if ext in (".res", ".scn"):
                    try:
                        from engines.godot_pck import GodotPckEngine
                        # Use the same extraction format as patch_inline_strings_safe
                        items.extend(GodotPckEngine._extract_scn_strings(
                            GodotPckEngine(), filepath.read_bytes(), rel))
                    except Exception:
                        pass
                    continue

                try:
                    content = filepath.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                if ext in (".tscn", ".tres") or ".tscn.remap" in fname or ".tres.remap" in fname:
                    items.extend(_extract_tscn(content, rel))
                elif ext == ".dtl":
                    from engines.godot_pck import extract_dtl_text
                    items.extend(extract_dtl_text(content, rel))
                elif ext == ".gd":
                    items.extend(_extract_gd(content, rel))
                elif ext == ".csv":
                    items.extend(_extract_csv(content, rel))
                elif ext in (".translation", ".po"):
                    items.extend(_extract_po(content, rel))
                else:
                    items.extend(_extract_generic(content, rel))

        return items

    def _extract_binary_res(self, filepath: Path, rel: str) -> list[TextItem]:
        """从 Godot 二进制 .res 文件中提取文本。"""
        data = filepath.read_bytes()
        items = []

        try:
            strings = _parse_godot_variant_strings(data)
            for offset, text in strings:
                if is_translatable(text):
                    items.append(TextItem(
                        file=rel, key=f"bin_{offset}", original=text,
                        meta={"byte_offset": offset},
                    ))
        except Exception as e:
            debug(f"二进制 .res 解析失败 ({rel}): {e}")

        return items


# ---------------------------------------------------------------------------
# Godot 4 二进制 Variant 解析（简化版，提取所有字符串）
# ---------------------------------------------------------------------------

def _parse_godot_variant_strings(data: bytes) -> list[tuple[int, str]]:
    """扫描二进制数据中的 Godot 4 Variant 字符串。返回 [(byte_offset, text), ...]."""
    results = []
    pos = 0
    limit = len(data)
    while pos < limit - 4:
        vtype = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        if vtype == 4:  # VAR_STRING
            if pos + 4 > limit:
                break
            strlen = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if strlen > 0 and strlen <= 5000 and pos + strlen <= limit:
                try:
                    text = data[pos:pos + strlen].decode("utf-8")
                    if text.strip():
                        results.append((pos, text))
                except UnicodeDecodeError:
                    pass
                pos += strlen
                # 跳过 padding
                if pos < limit and data[pos:pos + 4] == b"\x00\x00\x00\x00":
                    pos += 4

        elif vtype == 18:  # VAR_STRING_NAME
            if pos + 4 > limit:
                break
            strlen = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if strlen > 0 and strlen <= 500 and pos + strlen <= limit:
                try:
                    text = data[pos:pos + strlen].decode("utf-8")
                    if text.strip():
                        results.append((pos, text))
                except UnicodeDecodeError:
                    pass
                pos += strlen
                if pos < limit and data[pos:pos + 4] == b"\x00\x00\x00\x00":
                    pos += 4

        elif vtype in (0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20,
                       21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37,
                       38, 39, 40, 41, 42):
            # 已知简单类型，跳过固定字节数
            skip = {
                0: 0,  1: 4,  2: 8,  3: 8,  5: 8,  6: 8,  7: 8,  8: 8,  9: 8,
                10: 4, 11: 4, 12: 4, 13: 8, 14: 8, 15: 8, 16: 8, 17: 16,
                19: 12, 20: 8, 21: 4, 22: 8, 23: 4, 24: 8,
                25: 8, 26: 8, 27: 8, 28: 8, 29: 8,
                30: 8, 31: 8, 32: 8, 33: 8, 34: 8, 35: 8,
                36: 8, 37: 8, 38: 8, 39: 8, 40: 8, 41: 8, 42: 8,
            }.get(vtype)
            if skip is not None:
                pos += skip
            else:
                pos += 8  # 默认跳过

        else:
            # 未知类型或加密数据，跳过 1 字节重新对齐
            pos += 1

    return results


def _unpad_pkcs7(data: bytes) -> bytes:
    """移除 PKCS7 填充。"""
    if not data:
        return data
    pad_len = data[-1]
    if 0 < pad_len <= 16:
        # 检查填充是否有效
        if all(b == pad_len for b in data[-pad_len:]):
            return data[:-pad_len]
    return data


# ---------------------------------------------------------------------------
# 注册引擎（在 GodotEngine 之前，UUID 优先匹配）
# ---------------------------------------------------------------------------

registry.register(GodotFridaEngine())
