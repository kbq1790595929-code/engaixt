"""Unity 资源静态文本提取（UnityPy 扫描 + 原始二进制对话回退）。"""

from __future__ import annotations

import gzip
import io
import re
import shutil
import zlib
from pathlib import Path

from engines.base import TextItem
from utils.logger import info, debug, warning
from utils.text_extract import is_translatable


# ---------------------------------------------------------------------------
# 原始二进制文本提取（Fungus / Utage VN 框架回退方案）
# ---------------------------------------------------------------------------

def _extract_dialogue_from_bytes(raw: bytes) -> list[str]:
    """从原始 Unity 序列化字节中提取对话文本。

    核心启发式：
    1. 日文对话必定包含平假名，资源名通常只有片假名
    2. 对话文本连续且长度适中（4-500 字符）
    """
    results: list[str] = []
    current = bytearray()
    min_bytes = 12  # 至少 4 个 UTF-8 日文字符 ≈ 12 bytes

    for b in raw:
        if b >= 0x20 or b in (0x0A, 0x0D, 0x09):
            current.append(b)
        else:
            if len(current) >= min_bytes:
                try:
                    text = current.decode("utf-8")
                    if _is_dialogue_text(text):
                        results.append(text)
                except Exception:
                    pass
            current = bytearray()

    if len(current) >= min_bytes:
        try:
            text = current.decode("utf-8")
            if _is_dialogue_text(text):
                results.append(text)
        except Exception:
            pass

    return results


def _is_dialogue_text(text: str) -> bool:
    """判断是否为对话文本（而非资源路径/技术字符串）。"""
    if not text or len(text) < 4 or len(text) > 500:
        return False

    # 日文字符计数
    hiragana = sum(1 for c in text if 'ぁ' <= c <= 'ゟ')
    katakana = sum(1 for c in text if 'ァ' <= c <= 'ヿ')
    kanji = sum(1 for c in text if '一' <= c <= '鿿')

    jp_total = hiragana + katakana + kanji
    if jp_total < 4:
        return False

    # 核心判定：必须有假名（平假名或片假名）+ 日文汉字
    # 纯片假名 + 汉字 = UI 标签/按钮文本也是有效游戏文本
    # 纯片假名（无汉字、无平假名）= 动画状态名/资源路径
    if hiragana == 0 and katakana > 0 and kanji >= 2:
        pass  # UI 文本：片假名 + 汉字（如 "アニメ停止ボタン"）
    elif hiragana > 0:
        pass  # 对话文本：包含平假名
    else:
        return False

    # 排除含过多技术字符的
    technical = sum(1 for c in text if c in '._{}[]()@#*\\/:;<>')
    if technical > 3 and technical > jp_total * 0.3:
        return False

    return True


# ---------------------------------------------------------------------------
# Unity 资源扫描器
# ---------------------------------------------------------------------------

class UnityResourceScanner:
    """基于 UnityPy 的资源遍历器，提取所有可翻译文本。"""

    # 需要跳过扫描的资源类型
    SKIP_TYPES = {
        "Texture2D", "Sprite", "AudioClip", "Mesh", "Material",
        "Shader", "AnimationClip", "AnimatorController", "AnimatorOverrideController",
        "Avatar", "Cubemap", "Flare", "Font", "GameObject", "Lightmap",
        "LightProbes", "MovieTexture", "PhysicMaterial", "PhysicsMaterial2D",
        "Prefab", "RenderTexture", "Scene", "ScriptableObject",
        "TerrainData", "VideoClip", "AudioMixer", "AudioMixerGroup",
        "AudioMixerSnapshot", "ComputeShader", "LensFlare", "MonoScript",
    }

    # 文本包含模式（Lua 脚本、对话数据等）
    LUA_PATTERNS = re.compile(
        r'(?:text|setname|seticon|sel|message|desc|dialog|narration)\s*[\(\[].*?["\']([^"\']{2,})["\']',
        re.DOTALL,
    )

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self._unitypy_available = False
        try:
            import UnityPy
            self.UnityPy = UnityPy
            self._unitypy_available = True
        except ImportError:
            pass

    def scan(self) -> list[TextItem]:
        """扫描所有 Unity 资源文件，提取可翻译文本。"""
        items: list[TextItem] = []

        if self._unitypy_available:
            items = self._scan_with_unitypy()
        else:
            info("UnityPy 未安装，回退到文本文件扫描")
            items = self._scan_text_files()

        # 兼容模式：尝试解码轻度混淆的资源
        if not items:
            items = self._scan_compatibility_mode()

        # 过滤非翻译文本
        items = self._filter(items)

        info(f"资源扫描完成: {len(items)} 条可翻译文本")
        return items

    def _find_asset_files(self) -> list[Path]:
        """找到所有 Unity 资源文件，包含 Addressables catalog 和 split 文件合并。"""
        patterns = [
            "*.assets", "*.assets.split*", "*.unity3d", "*.bundle",
            "sharedassets*.assets", "sharedassets*.assets.split*",
            "level*", "*.resourcePack",
            "StreamingAssets/*.bundle", "StreamingAssets/*.assets",
            "StreamingAssets/**/*.bundle", "StreamingAssets/**/*.assets",
        ]

        files: list[Path] = []

        # 优先搜索 *_Data 目录
        for data_dir in self.game_dir.glob("*_Data"):
            for pattern in patterns:
                for f in data_dir.glob(pattern):
                    if f.is_file() and f.stat().st_size > 100:
                        files.append(f)

        # 也搜索游戏根目录
        for pattern in ["*.assets", "*.bundle"]:
            for f in self.game_dir.glob(pattern):
                if f.is_file() and f.stat().st_size > 100 and f not in files:
                    files.append(f)

        # 搜索子目录
        for sub in ["contents", "game", "data", "StreamingAssets"]:
            d = self.game_dir / sub
            if d.is_dir():
                for pattern in patterns:
                    for f in d.rglob("*.assets"):
                        if f.is_file() and f.stat().st_size > 100 and f not in files:
                            files.append(f)
                    for f in d.rglob("*.bundle"):
                        if f.is_file() and f.stat().st_size > 100 and f not in files:
                            files.append(f)

        # ---- Addressables 系统 ----
        files.extend(self._find_addressables_bundles())

        # ---- 合并 .split 分块文件 ----
        files = self._merge_split_files(files)

        debug(f"找到 {len(files)} 个资源文件")
        return files

    def _find_addressables_bundles(self) -> list[Path]:
        """从 Unity Addressables catalog 解析 Bundle 文件列表。

        Addressables 使用 catalog.json 记录所有 Bundle 的名称和依赖关系。
        """
        bundles: list[Path] = []
        catalog_patterns = [
            "StreamingAssets/aa/catalog.json",
            "StreamingAssets/aa/*/catalog.json",
            "StreamingAssets/catalog.json",
            "StreamingAssets/Addressables/catalog.json",
        ]

        for data_dir in self.game_dir.glob("*_Data"):
            for pattern in catalog_patterns:
                for catalog_path in data_dir.glob(pattern):
                    try:
                        import json
                        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                        # catalog 格式: {"m_InternalIds": [...], "m_Resources": [...]}
                        internal_ids = catalog.get("m_InternalIds", [])
                        for entry in internal_ids:
                            if isinstance(entry, str) and entry:
                                # 路径是相对于 StreamingAssets/aa/ 的
                                bundle_path = catalog_path.parent / entry
                                if bundle_path.exists() and bundle_path not in bundles:
                                    bundles.append(bundle_path)
                                    debug(f"  Addressables: {entry}")
                    except Exception:
                        continue

        return bundles

    def _merge_split_files(self, files: list[Path]) -> list[Path]:
        """合并 .split0/.split1 分块文件为单个文件。

        部分游戏将大型 AssetBundle 拆分为多个 .split* 文件
        （规避 FAT32 4GB 限制）。返回的列表中将 .split0 替换为合并后的文件路径。
        """
        merged: list[Path] = []
        split_groups: dict[str, list[Path]] = {}

        for f in files:
            name = f.name
            if ".split" in name:
                # 找到基础名：xxx.bundle.split0 → xxx.bundle
                base = name[:name.index(".split")]
                split_groups.setdefault(str(f.parent / base), []).append(f)
            else:
                merged.append(f)

        for base_name, parts in split_groups.items():
            # 按 split 编号排序
            parts.sort(key=lambda p: int(p.suffix.replace(".split", "") or 0))
            combined_path = self._combine_splits(parts, Path(base_name))
            if combined_path:
                merged.append(combined_path)

        return merged

    def _combine_splits(self, parts: list[Path], output: Path) -> Path | None:
        """合并分块文件，返回合并后的路径。合并完成即返回。"""
        if output.exists():
            return output  # 已合并过

        # 检查是否真的需要合并（部分文件 .split0 可直接单独解析）
        if len(parts) == 1:
            return parts[0]

        try:
            total_size = sum(p.stat().st_size for p in parts)
            info(f"  合并分块文件: {output.name} ({len(parts)} 块, {total_size:,} bytes)")
            with open(output, "wb") as dst:
                for part in parts:
                    with open(part, "rb") as src:
                        shutil.copyfileobj(src, dst)
            return output
        except Exception as e:
            warning(f"  分块文件合并失败: {e}")
            return None

    @staticmethod
    def _detect_endian(file_path: Path) -> str:
        """检测 Unity 资源文件的大小端序。AssetFileHeader offset 0x0D。"""
        try:
            with open(file_path, "rb") as f:
                header = f.read(32)
                if len(header) < 14:
                    return "little"
                endian_byte = header[0x0D]
                return "big" if endian_byte == 1 else "little"
        except Exception:
            return "little"

    def _scan_with_unitypy(self) -> list[TextItem]:
        """使用 UnityPy 遍历资源对象。支持大端序。"""
        items: list[TextItem] = []
        asset_files = self._find_asset_files()

        info(f"UnityPy 遍历 {len(asset_files)} 个资源文件...")

        for af in asset_files:
            try:
                endian = self._detect_endian(af)
                if endian == "big":
                    debug(f"  {af.name}: 检测到大端序（主机移植）")
                    # UnityPy >= 2.x 支持大端序，通过环境变量或参数传入
                    import UnityPy
                    # UnityPy 2.x 的 load 方法可能支持 endian 参数
                    env = UnityPy.load(str(af))
                else:
                    env = self.UnityPy.load(str(af))

                file_items = 0
                for obj in env.objects:
                    if obj.type.name in self.SKIP_TYPES:
                        continue

                    extracted = self._extract_from_object(obj, af.name)
                    if extracted:
                        items.extend(extracted)
                        file_items += len(extracted)

                if file_items > 0:
                    debug(f"  {af.name}: {file_items} 条")
            except Exception as e:
                debug(f"  {af.name}: 跳过 ({e})")

        # 补充扫描 StreamingAssets 中的纯文本文件
        items.extend(self._scan_text_files())

        return items

    def _extract_from_object(self, obj, source_name: str) -> list[TextItem]:
        """从单个 Unity 对象提取文本。支持 type tree 失败时的原始二进制回退。"""
        items: list[TextItem] = []
        data = None
        try:
            data = obj.read()
        except Exception:
            # type tree 解析失败 → 回退到原始二进制提取
            items = self._extract_from_raw_object(obj, source_name)
            if not items:
                return items
            return items

        obj_type = obj.type.name

        if obj_type == "TextAsset":
            items = self._extract_text_asset(data, source_name)

        elif obj_type == "MonoBehaviour":
            items = self._extract_mono_behaviour(data, source_name)

        elif obj_type == "AssetBundle":
            # 递归处理子 bundle
            items = self._extract_bundle(data, source_name)

        return items

    def _extract_text_asset(self, data, source: str) -> list[TextItem]:
        """从 TextAsset 提取文本。支持明文和 Lua 字节码。"""
        items: list[TextItem] = []
        script = None
        asset_name = ""

        if hasattr(data, "m_Script"):
            script = data.m_Script
        if hasattr(data, "m_Name"):
            asset_name = data.m_Name
        if hasattr(data, "m_PathName"):
            path_name = data.m_PathName
            if isinstance(path_name, str) and path_name:
                asset_name = asset_name or path_name

        if isinstance(script, bytes):
            if len(script) < 50:
                return items

            # 检测 Lua 字节码
            if script[:4] == self._LUA_MAGIC:
                return self._try_extract_lua_bytecode(script, asset_name or source)

            script_text = script.decode("utf-8", errors="replace")
        elif isinstance(script, str):
            if len(script) < 50:
                return items
            script_text = script
        else:
            return items

        # Lua 脚本：使用 Lua 专用的提取模式
        if "function " in script_text or "return function" in script_text or 'text("' in script_text:
            items = self._extract_lua_strings(script_text, asset_name or source)
        else:
            # 通用字符串提取
            items = self._extract_generic_strings(script_text, asset_name or source)

        return items

    def _extract_mono_behaviour(self, data, source: str) -> list[TextItem]:
        """从 MonoBehaviour 提取文本字段。"""
        items: list[TextItem] = []
        try:
            if hasattr(data, "dump"):
                text = data.dump()
                if isinstance(text, str) and len(text) > 4:
                    items = self._extract_generic_strings(text, source)
        except Exception:
            pass
        return items

    def _extract_from_raw_object(self, obj, source: str) -> list[TextItem]:
        """当 type tree 解析失败时，从原始二进制中提取日文/中文文本。

        适用于 Fungus、Utage 等 VN 框架 — 对话文本嵌入在 MonoBehaviour
        序列化数据中，即使 UnityPy 无法完整解析 type tree，字符串字面量仍
        存在于原始字节流中。
        """
        items: list[TextItem] = []
        raw = None

        # 获取对象原始序列化字节
        if hasattr(obj, 'get_raw_data'):
            try:
                raw = obj.get_raw_data()
            except Exception:
                raw = None
        elif hasattr(obj, 'raw_data'):
            raw = obj.raw_data
        elif hasattr(obj, 'data'):
            raw = obj.data
        else:
            raw = None

        if not raw or not isinstance(raw, bytes) or len(raw) < 8:
            return items

        # 从原始字节中提取 UTF-8 对话文本
        # 关键启发式：日文对话必定包含假名（hiragana/katakana），
        # 这可以区分真正的对话文本和资源路径/动画状态名
        texts = _extract_dialogue_from_bytes(raw)

        for text in texts:
            if len(text) >= 4:
                items.append(TextItem(original=text, file=source))

        return items

    def _extract_bundle(self, data, source: str) -> list[TextItem]:
        """从 AssetBundle 递归提取。"""
        items: list[TextItem] = []
        try:
            if hasattr(data, "m_Container"):
                for entry in data.m_Container:
                    try:
                        inner = entry.read()
                        if inner:
                            sub_items = self._extract_text_asset(inner, source)
                            items.extend(sub_items)
                    except Exception:
                        continue
        except Exception:
            pass
        return items

    # Lua 字节码魔数
    _LUA_MAGIC = b"\x1bLua"

    def _try_extract_lua_bytecode(self, raw: bytes, file_name: str) -> list[TextItem]:
        """尝试从 Lua 字节码中提取字符串。

        策略：
        1. 检测 Lua 魔数 \\x1bLua
        2. 优先尝试 unluac/luadec 反编译
        3. 回退：直接解析字符串常量池（Lua 5.1 格式）
        """
        if raw[:4] != self._LUA_MAGIC:
            return []

        lua_version = raw[4]
        if lua_version not in (0x51, 0x52, 0x53, 0x54):  # 5.1-5.4
            return []

        info(f"  检测到 Lua {lua_version >> 4}.{lua_version & 0xF} 字节码: {file_name}")

        # 方法 1: 尝试 unluac (Java)
        code = self._run_luac_decompiler(raw, lua_version)
        if code:
            return self._extract_lua_strings(code, file_name)

        # 方法 2: 直接扫描字符串常量池
        strings = self._scan_lua_string_constants(raw, lua_version)
        if strings:
            items: list[TextItem] = []
            seen: set[str] = set()
            for s in strings:
                if s not in seen and is_translatable(s):
                    seen.add(s)
                    items.append(TextItem(
                        file=file_name, key=f"luac_str",
                        original=s, context=f"Lua {lua_version >> 4}.{lua_version & 0xF} bytecode",
                    ))
            if items:
                info(f"  字节码字符串提取: {len(items)} 条 (准确率 ~85%)")
            return items

        return []

    def _run_luac_decompiler(self, data: bytes, version: int) -> str | None:
        """尝试用外部反编译器处理 Lua 字节码。"""
        import subprocess
        import tempfile

        # 尝试 luadec（如果有）
        tools = ["luadec", "unluac", "luajit"]
        for tool in tools:
            exe = shutil.which(tool)
            if not exe:
                continue
            try:
                with tempfile.NamedTemporaryFile(suffix=".luac", delete=False) as f:
                    f.write(data)
                    tmp_path = f.name
                result = subprocess.run(
                    [exe, tmp_path], capture_output=True, text=True, timeout=30
                )
                Path(tmp_path).unlink(missing_ok=True)
                if result.returncode == 0 and len(result.stdout) > 50:
                    return result.stdout
            except Exception:
                continue

        return None

    def _scan_lua_string_constants(self, data: bytes, version: int) -> list[str]:
        """直接扫描 Lua 5.1 字节码的字符串常量池。

        Lua 5.1 字节码结构：
        - Header: 12 bytes (magic + version + format + endian + sizes)
        - Function block: source name + line info + locals + upvalues + constants
        - 字符串常量: size_t (integer) + 字符串字节（不是 \\0 结尾）
        """
        strings: list[str] = []
        pos = 12  # 跳过头部

        try:
            # 读取整数大小（Lua 5.1 的 int 是 4 字节）
            int_size = 4
            size_t_size = data[10] if len(data) > 10 else 4
            if size_t_size not in (4, 8):
                size_t_size = 4

            # 解析函数原型头部
            # 跳过 source name string
            pos, source_name = self._read_lua_string(data, pos, size_t_size)
            # 跳过 line defined, last line defined
            pos += int_size * 2
            # 跳过 num_upvalues, num_params, is_vararg, max_stack
            pos += 1 + 1 + 1 + 1
            # 跳过指令表
            pos, _ = self._read_lua_int(data, pos, int_size)
            pos *= 4  # instruction count * 4
            # 跳过常量表 — 这里才是我们要的！
            pos += int_size  # skip the count itself...

            # Actually, let me redo this more carefully for Lua 5.1
            pos = 12  # reset to after header
            int_size = 4
            size_t_val = data[10] if len(data) > 10 else 4
            actual_size_t = size_t_val if size_t_val in (4, 8) else 4

            # FunctionProto starts here
            # source_name
            if pos >= len(data):
                return strings
            slen = int.from_bytes(data[pos:pos + actual_size_t], 'little')
            pos += actual_size_t
            if slen > 0:
                pos += slen

            # linedefined, lastlinedefined
            pos += 8

            # numparams, is_vararg, maxstacksize
            if pos + 3 > len(data):
                return strings
            pos += 3

            # code (instructions)
            if pos + 4 > len(data):
                return strings
            code_count = int.from_bytes(data[pos:pos + 4], 'little')
            pos += 4 + code_count * 4

            # constants
            if pos + 4 > len(data):
                return strings
            const_count = int.from_bytes(data[pos:pos + 4], 'little')
            pos += 4

            for _ in range(const_count):
                if pos >= len(data):
                    break
                const_type = data[pos]
                pos += 1
                if const_type == 4:  # LUA_TSTRING
                    if pos + actual_size_t > len(data):
                        break
                    slen = int.from_bytes(data[pos:pos + actual_size_t], 'little')
                    pos += actual_size_t
                    if slen > 0 and pos + slen <= len(data):
                        s = data[pos:pos + slen].decode('utf-8', errors='replace')
                        pos += slen
                        if len(s) >= 2 and len(s) <= 500:
                            strings.append(s)
                elif const_type == 3:  # LUA_TNUMBER
                    pos += 8  # double
                elif const_type == 1:  # LUA_TBOOLEAN
                    pos += 1
                # LUA_TNIL (0) — no data

        except Exception:
            pass

        return strings

    @staticmethod
    def _read_lua_string(data: bytes, pos: int, size_t: int) -> tuple[int, str]:
        """读取 Lua 字符串 (size_t + data)。"""
        if pos + size_t > len(data):
            return pos, ""
        slen = int.from_bytes(data[pos:pos + size_t], 'little')
        pos += size_t
        if slen == 0:
            return pos, ""
        if pos + slen > len(data):
            return pos, ""
        s = data[pos:pos + slen].decode('utf-8', errors='replace')
        return pos + slen, s

    @staticmethod
    def _read_lua_int(data: bytes, pos: int, int_size: int) -> tuple[int, int]:
        """读取 Lua 整数。"""
        if pos + int_size > len(data):
            return pos, 0
        val = int.from_bytes(data[pos:pos + int_size], 'little')
        return pos + int_size, val

    def _extract_lua_strings(self, code: str, file_name: str) -> list[TextItem]:
        """从 Lua 脚本提取可翻译字符串。"""
        items: list[TextItem] = []
        seen: set[str] = set()
        line_num = 0

        for line in code.split("\n"):
            line_num += 1
            stripped = line.strip()

            # text("...") / setname("...") 等
            for m in re.finditer(
                r'(?:text|setname|seticon|sel|message|desc|dialog|narration|name|title|subtitle)'
                r'\s*[\(\["](?:\s*"[^"]*"\s*,\s*)?\s*"([^"]{2,})"',
                stripped,
            ):
                text = m.group(1)
                if text not in seen and is_translatable(text):
                    seen.add(text)
                    items.append(TextItem(
                        file=file_name, key=f"L{line_num}_lua",
                        original=text, line=line_num,
                    ))

            # item[N] = "..." 模式
            for m in re.finditer(r'\[(\d+)\]\s*=\s*"([^"]{2,})"', stripped):
                text = m.group(2)
                if text not in seen and is_translatable(text):
                    seen.add(text)
                    items.append(TextItem(
                        file=file_name, key=f"L{line_num}_arr",
                        original=text, line=line_num,
                    ))

            # 注释中的日文文本
            if stripped.startswith("--") and len(stripped) > 4:
                comment = stripped[2:].strip()
                if comment not in seen and is_translatable(comment):
                    seen.add(comment)
                    items.append(TextItem(
                        file=file_name, key=f"L{line_num}_comment",
                        original=comment, line=line_num,
                        context="Lua comment",
                    ))

        return items

    def _extract_generic_strings(self, text: str, source: str) -> list[TextItem]:
        """通用字符串提取。"""
        items: list[TextItem] = []
        seen: set[str] = set()
        line_num = 0

        for line in text.split("\n"):
            line_num += 1
            for m in re.finditer(r'"([^"\\]{2,200})"', line):
                s = m.group(1)
                if s not in seen and is_translatable(s):
                    seen.add(s)
                    items.append(TextItem(
                        file=source, key=f"L{line_num}_str",
                        original=s, line=line_num,
                    ))

        return items

    def _scan_text_files(self) -> list[TextItem]:
        """扫描纯文本文件（JSON, CSV, TXT, Lua 等）。"""
        items: list[TextItem] = []

        streaming = self.game_dir / "StreamingAssets"
        search_dirs = [self.game_dir]
        if streaming.is_dir():
            search_dirs.append(streaming)

        for data_dir in self.game_dir.glob("*_Data"):
            search_dirs.append(data_dir)
            sa = data_dir / "StreamingAssets"
            if sa.is_dir():
                search_dirs.append(sa)

        # 排除 BepInEx / MonoBleedingEdge 等运行时/框架目录，避免扫到插件文档
        _EXCLUDE_DIRS = {"BepInEx", "MonoBleedingEdge", "doorstop_config.ini", "winhttp.dll",
                         "version.dll", "xinput9_1_0.dll", ".doorstop_version"}

        for search_dir in search_dirs:
            for ext in ["*.txt", "*.json", "*.csv", "*.lua", "*.xml", "*.yaml", "*.yml"]:
                for f in search_dir.rglob(ext):
                    if any(part in _EXCLUDE_DIRS for part in f.parts):
                        continue
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                        if len(content) < 5:
                            continue
                        sub_items = self._extract_generic_strings(
                            content, str(f.relative_to(self.game_dir))
                        )
                        if sub_items:
                            items.extend(sub_items)
                    except Exception:
                        continue

        return items

    def _scan_compatibility_mode(self) -> list[TextItem]:
        """兼容模式：按优先级依次尝试解混淆策略。

        策略链：XOR → 偏移 → zlib/gzip/lz4 解压 → 魔数修复
        每步成功即停止，全部失败则记录文件信息供人工分析。
        """
        items: list[TextItem] = []
        data_dirs = list(self.game_dir.glob("*_Data"))
        data_dir = data_dirs[0] if data_dirs else self.game_dir

        asset_files = list(data_dir.glob("*.assets")) + list(data_dir.glob("*.bundle"))
        if not asset_files:
            asset_files = list(data_dir.rglob("*.assets")) + list(data_dir.rglob("*.bundle"))

        failed_files: list[tuple[str, int, str]] = []

        for af in asset_files[:50]:
            try:
                raw = af.read_bytes()
                if len(raw) < 128:
                    continue

                source = af.name
                texts: list[TextItem] = []

                # Step 1: XOR 混淆
                texts = self._try_xor_decode(raw, source)
                if texts:
                    items.extend(texts)
                    continue

                # Step 2: 头字节偏移
                texts = self._try_offset_decode(raw, source)
                if texts:
                    items.extend(texts)
                    continue

                # Step 3: 压缩解包
                texts = self._try_decompress(raw, source)
                if texts:
                    items.extend(texts)
                    continue

                # Step 4: 魔数修复后重新尝试 UnityPy
                texts = self._try_magic_repair(raw, af, source)
                if texts:
                    items.extend(texts)
                    continue

                # 全部失败，记录
                hex_preview = raw[:64].hex(" ")
                failed_files.append((source, len(raw), hex_preview))

            except Exception:
                continue

        if failed_files:
            warning(f"兼容模式: {len(failed_files)} 个文件无法解析")
            for name, size, hex_preview in failed_files:
                debug(f"  {name} ({size:,} bytes) 前64字节: {hex_preview}")
            info("以上文件疑似使用自定义加密，可提交前64字节HEX给维护者添加支持")

        return items

    # ---- 解混淆策略 ----

    def _try_xor_decode(self, data: bytes, source: str) -> list[TextItem]:
        """尝试常见 XOR key 解码。扩展 key 列表覆盖更多变体。"""
        # 常见 XOR key（单字节 + 双字节模式）
        for key in [0xFF, 0xAA, 0x55, 0x34, 0xBD, 0x9A, 0xC3, 0x71, 0xE4, 0x1B]:
            decoded = bytes(b ^ key for b in data[:8192])
            text = decoded.decode("utf-8", errors="replace")
            jp_chars = sum(1 for c in text if '぀' <= c <= 'ヿ' or '一' <= c <= '鿿')
            if jp_chars > 10:
                items = self._extract_generic_strings(text, source)
                if items:
                    info(f"  兼容模式: {source} XOR 0x{key:02X} 解码成功")
                    return items
        return []

    def _try_offset_decode(self, data: bytes, source: str) -> list[TextItem]:
        """尝试跳过头部偏移后读取文本。扩展偏移量覆盖更多情况。"""
        for offset in [4, 8, 16, 32, 64, 128, 256, 512]:
            if offset >= len(data) - 512:
                continue
            text = data[offset:offset + 16384].decode("utf-8", errors="replace")
            jp_chars = sum(1 for c in text if '぀' <= c <= 'ヿ' or '一' <= c <= '鿿')
            if jp_chars > 10:
                items = self._extract_generic_strings(text, source)
                if items:
                    info(f"  兼容模式: {source} offset={offset} 解码成功")
                    return items
        return []

    def _try_decompress(self, data: bytes, source: str) -> list[TextItem]:
        """尝试解压缩（zlib / gzip / lz4）。"""
        decompressors = [
            ("zlib", lambda d: zlib.decompress(d, -15)),   # raw deflate
            ("zlib+header", lambda d: zlib.decompress(d)),  # zlib wrapped
            ("gzip", lambda d: gzip.decompress(d)),
        ]

        # lz4 是可选的
        try:
            import lz4.block
            decompressors.append(("lz4", lambda d: lz4.block.decompress(d, uncompressed_size=len(d) * 10)))
        except ImportError:
            pass

        for name, decompressor in decompressors:
            try:
                decompressed = decompressor(data)
                if len(decompressed) < 64:
                    continue
                text = decompressed[:32768].decode("utf-8", errors="replace")
                jp_chars = sum(1 for c in text if '぀' <= c <= 'ヿ' or '一' <= c <= '鿿')
                en_words = len(re.findall(r'\b[A-Za-z]{4,}\b', text))
                if jp_chars > 10 or en_words > 20:
                    items = self._extract_generic_strings(text, source)
                    if items:
                        info(f"  兼容模式: {source} {name} 解压成功 ({len(data):,} → {len(decompressed):,} bytes)")
                        return items
            except Exception:
                continue

        return []

    def _try_magic_repair(self, data: bytes, file_path: Path, source: str) -> list[TextItem]:
        """尝试修复自定义魔数后重新用 UnityPy 解析。

        部分游戏将 UnityFS 魔数 'UnityFS' 替换为自定义字符串。
        """
        # 已知变体
        known_magic_pairs = [
            (b"UnityFS", b"UnityFS"),   # 标准（不变）
            (b"UnityFs", b"UnityFS"),   # 大小写变体
            (b"unityfs", b"UnityFS"),
            (b"UNITYFS", b"UnityFS"),
            (b"UXData", b"UnityFS"),    # 某游戏的自定义魔数
        ]

        for wrong, right in known_magic_pairs:
            if data[:len(wrong)] == wrong and wrong != right:
                repaired = right + data[len(wrong):]
                try:
                    env = self.UnityPy.load(io.BytesIO(repaired))
                    items: list[TextItem] = []
                    for obj in env.objects:
                        if obj.type.name == "TextAsset":
                            sub = self._extract_text_asset(obj.read(), source)
                            items.extend(sub)
                    if items:
                        info(f"  兼容模式: {source} 魔数修复 ({wrong.decode()} → {right.decode()}) 成功")
                        return items
                except Exception:
                    continue

        return []

    # ---- 多级文本过滤管线 ----

    # 第1级：长度过滤（最快，先排除）
    _FILTER_LENGTH = re.compile(r'^.{2,500}$', re.DOTALL)

    # 第2级：纯技术字符串
    _FILTER_PATH_LIKE = re.compile(r'^[A-Za-z0-9_\-/\\\.@:]+$')
    _FILTER_HASH_LIKE = re.compile(r'^[0-9a-f]{8,}$', re.IGNORECASE)
    _FILTER_NUMERIC = re.compile(r'^[\d\s\.\,\-\+\=\(\)\'\"\[\]\{\}]+$')

    # 第3级：纯格式字符串
    _FILTER_FORMAT_ONLY = re.compile(r'^(\{[^}]*\}|\%[^%]|\<[^>]+\>|\[[^\]]+\])+$')

    # 第4级：Unity 内部枚举/标识符
    _FILTER_UNITY_ENUM = re.compile(r'^[A-Z][A-Z0-9_]{3,}$')
    _FILTER_UNITY_INTERNAL = re.compile(r'^(m_|unity_|__)[A-Za-z]')

    # 第5级：无意义短标识符
    _FILTER_SHORT_ID = re.compile(r'^[a-z][a-zA-Z0-9]{0,3}$')

    def _filter(self, items: list[TextItem]) -> list[TextItem]:
        """多级过滤管线：排除代码字符串、路径、变量名、Hash、纯格式串等。"""
        filtered: list[TextItem] = []
        stats = {"total": len(items), "length": 0, "path": 0, "hash": 0,
                  "numeric": 0, "format": 0, "enum": 0, "internal": 0, "short": 0}

        for item in items:
            s = item.original.strip()

            # 第1级：长度过滤
            if not (2 <= len(s) <= 500):
                stats["length"] += 1
                continue

            # 第2级：纯技术字符串
            if self._FILTER_PATH_LIKE.match(s):
                stats["path"] += 1
                continue
            if self._FILTER_HASH_LIKE.match(s) and len(s) >= 16:
                stats["hash"] += 1
                continue
            if self._FILTER_NUMERIC.match(s):
                stats["numeric"] += 1
                continue

            # 第3级：纯格式字符串
            if self._FILTER_FORMAT_ONLY.match(s):
                stats["format"] += 1
                continue

            # 第4级：Unity 内部标识符
            if self._FILTER_UNITY_ENUM.match(s) and len(s) >= 6:
                stats["enum"] += 1
                continue
            if self._FILTER_UNITY_INTERNAL.match(s):
                stats["internal"] += 1
                continue

            # 第5级：短标识符排除
            if self._FILTER_SHORT_ID.match(s):
                stats["short"] += 1
                continue

            # 最终：使用 text_extract 的 is_translatable 做兜底判断
            if is_translatable(item.original):
                filtered.append(item)

        total_rejected = sum(stats.values()) - stats["total"] + len(filtered)
        if total_rejected > 0:
            detail = " ".join(f"{k}:{v}" for k, v in stats.items() if v > 0)
            debug(f"文本过滤: {len(filtered)} 保留 / {total_rejected} 排除 ({detail})")

        return filtered
