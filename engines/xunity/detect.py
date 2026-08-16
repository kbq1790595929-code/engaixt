"""Unity 架构/版本/BCL 裁剪检测与 BepInEx 变体选择。"""

from __future__ import annotations

import re
import struct
from pathlib import Path

from utils.logger import debug, warning

from engines.xunity.constants import BEPINEX_SOURCES


# ---------------------------------------------------------------------------
# 架构检测
# ---------------------------------------------------------------------------

class UnityArchitecture:
    """Unity 游戏架构信息。"""
    mono = "mono"
    il2cpp = "il2cpp"
    unknown = "unknown"


def detect_unity_arch(game_dir: Path) -> str:
    """检测 Unity 游戏架构：Mono 或 IL2CPP。"""
    # IL2CPP: GameAssembly.dll
    if (game_dir / "GameAssembly.dll").exists():
        return UnityArchitecture.il2cpp

    # Mono: Managed/Assembly-CSharp.dll
    managed_dir = None
    for data_dir in game_dir.glob("*_Data"):
        managed = data_dir / "Managed"
        if managed.is_dir():
            managed_dir = managed
            break
    if not managed_dir:
        managed_dir = game_dir / "Managed"

    if managed_dir.is_dir() and (managed_dir / "Assembly-CSharp.dll").exists():
        return UnityArchitecture.mono

    # 子目录检查
    for sub in ["contents", "game", "data", "bin"]:
        d = game_dir / sub
        if d.is_dir():
            arch = detect_unity_arch(d)
            if arch != UnityArchitecture.unknown:
                return arch

    return UnityArchitecture.unknown


def detect_unity_bits(game_dir: Path) -> str:
    """检测 Unity 游戏是 32 位还是 64 位。

    PE 格式：DOS header → PE signature (4 bytes) → COFF header (20 bytes) → Optional Header.
    Optional Header 的前 2 字节是 Magic: 0x10B=PE32, 0x20B=PE32+.
    """
    import struct

    def _read_magic(filepath: Path) -> int | None:
        try:
            with open(filepath, "rb") as f:
                header = f.read(64)
                if header[:2] != b"MZ":
                    return None
                pe_offset = struct.unpack("<I", header[0x3C:0x40])[0]
                # PE sig (4) + COFF header (20) = 24 bytes → Optional Header Magic
                f.seek(pe_offset + 24)
                return struct.unpack("<H", f.read(2))[0]
        except Exception:
            return None

    # 检查主可执行文件的 PE 头
    for exe in game_dir.glob("*.exe"):
        magic = _read_magic(exe)
        if magic == 0x20B:
            return "x64"
        elif magic == 0x10B:
            return "x86"

    # 检查 GameAssembly.dll（IL2CPP 场景）
    ga = game_dir / "GameAssembly.dll"
    if ga.exists():
        magic = _read_magic(ga)
        if magic == 0x20B:
            return "x64"
        elif magic == 0x10B:
            return "x86"

    # 默认 x64
    return "x64"


def detect_unity_version(game_dir: Path) -> str:
    """检测 Unity 引擎版本号。

    依次尝试：
    1. globalgamemanagers header offset 0x14 的 ASCII 版本字符串
    2. UnityPlayer.dll .rdata 段中的版本正则匹配
    3. 回退返回 "unknown"
    """
    # 完整 Unity 版本号模式，如 6000.2.14f1 / 2021.3.0f1 / 5.6.7f1。
    # 主版本可为 1~4 位（Unity 6 起主版本为 6000）。
    _UNITY_VER_RE = re.compile(r"\d{1,4}\.\d+\.\d+[abcfp]\d+")

    # 方法 1: globalgamemanagers —— 在头部扫描版本字符串。
    # 不再依赖固定 offset 0x14：Unity 6 的序列化头格式变化会导致定偏移读取失败，
    # 进而错误回退到 UnityPlayer.dll 并匹配到无关的旧版本串（如 2018.3.0a1）。
    for data_dir in game_dir.glob("*_Data"):
        gg = data_dir / "globalgamemanagers"
        if gg.exists() and gg.stat().st_size > 32:
            try:
                with open(gg, "rb") as f:
                    head = f.read(512)
                text = head.decode("ascii", errors="replace")
                m = _UNITY_VER_RE.search(text)
                if m:
                    debug(f"Unity 版本 (globalgamemanagers): {m.group(0)}")
                    return m.group(0)
                # 兜底：旧的固定 offset 0x14 读取
                version = ""
                for b in head[0x14:0x14 + 32]:
                    if b == 0:
                        break
                    if 32 <= b < 127:
                        version += chr(b)
                if re.match(r"\d+\.\d+\.\d+", version):
                    debug(f"Unity 版本 (globalgamemanagers/offset): {version}")
                    return version
            except Exception:
                pass

    # 方法 2: UnityPlayer.dll .rdata 段
    up_dll = game_dir / "UnityPlayer.dll"
    if not up_dll.exists():
        up_dll = game_dir / "UnityPlayer_.dll"
    if up_dll.exists():
        try:
            data = up_dll.read_bytes()
            text = data.decode("latin-1", errors="ignore")
            cands = _UNITY_VER_RE.findall(text)
            if cands:
                # 选主版本号最大者（Unity6=6000 应胜过 dll 内残留的 2018 等内部串），
                # 同主版本时优先 release(f)。
                def _ver_key(v: str):
                    major = int(v.split(".")[0])
                    return (major, 1 if "f" in v else 0)
                best = max(cands, key=_ver_key)
                debug(f"Unity 版本 (UnityPlayer.dll): {best}")
                return best
        except Exception:
            pass

    # 方法 3: 子目录检查
    for sub in ["contents", "game", "data"]:
        d = game_dir / sub
        if d.is_dir():
            v = detect_unity_version(d)
            if v != "unknown":
                return v

    return "unknown"


def parse_unity_year(version: str) -> int:
    """从 Unity 版本字符串提取主版本年份。如 '2021.3.15f1' → 2021。"""
    m = re.match(r"(\d{4})\.", version)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d+)\.", version)
    if m:
        minor = int(m.group(1))
        # Unity 5.x 时代
        if 4 <= minor <= 5:
            return 5
        return 2017
    return 0


def parse_unity_version_tuple(version: str) -> tuple[int, int, int]:
    """Return (major, minor, patch) for Unity versions like 5.4.0 or 2020.3.12f1."""
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_legacy_unity_runtime_injection_blocked(game_dir: Path) -> tuple[bool, str]:
    """Unity 5.5 and older Mono games often break input when doorstop/BepInEx is injected."""
    version = detect_unity_version(game_dir)
    major, minor, _ = parse_unity_version_tuple(version)
    if major and major < 5:
        return True, version
    if major == 5 and minor <= 5:
        return True, version
    return False, version


# BepInEx/MonoMod/Harmony 运行时 hook 依赖的关键 BCL 方法。
# 部分 Unity 游戏开启 managed code stripping 后会裁掉方法体，导致注入失败。
# 注意：简单字符串搜索不可靠（方法名可能保留在元数据中），优先用 Mono.Cecil
# 验证方法体，回退时结合文件大小判断。
_HOOK_CRITICAL_METHODS = [
    ("System.Reflection.MethodBase", "GetMethodBody"),
    ("System.Reflection.Module", "ResolveType"),
    ("System.Reflection.Module", "ResolveMethod"),
    ("System.Reflection.Emit", "DynamicMethod"),
    ("System.Runtime.CompilerServices.RuntimeHelpers", "PrepareMethod"),
    ("System.Reflection.Module", "ResolveField"),
    ("System.Reflection.Module", "ResolveSignature"),
]

# mscorlib 小于此阈值视为可疑裁减（正常 Unity 2021+ Mono mscorlib ~4-6MB）
_MSCORLIB_MIN_SIZE = 2_500_000  # 2.5 MB


def detect_bcl_stripping(game_dir: Path) -> tuple[bool, list[str]]:
    """检测游戏 BCL 是否被激进裁减，导致 BepInEx 无法工作。

    优先使用 Mono.Cecil 验证方法体是否存在（最准确），
    回退时结合 mscorlib 文件大小作为启发式判断。

    返回 (is_stripped_severely, missing_methods)。
    is_stripped_severely=True 表示缺少关键方法体，运行时注入不可行。
    """
    managed_dir = None
    for data_dir in game_dir.glob("*_Data"):
        m = data_dir / "Managed"
        if m.is_dir():
            managed_dir = m
            break
    if not managed_dir:
        managed_dir = game_dir / "Managed"
    if not managed_dir.is_dir():
        return False, []

    mscorlib = managed_dir / "mscorlib.dll"
    if not mscorlib.exists():
        return False, []

    missing: list[str] = []

    # 策略 1：Mono.Cecil 精确检测方法体
    try:
        from Mono.Cecil import ModuleDefinition
        module = ModuleDefinition.ReadModule(str(mscorlib))
        for type_name, method_name in _HOOK_CRITICAL_METHODS:
            found_body = False
            for t in module.Types:
                if t.FullName == type_name:
                    for mtd in t.Methods:
                        if mtd.Name == method_name and mtd.HasBody:
                            found_body = True
                            break
                    break
            if not found_body:
                missing.append(f"{type_name}.{method_name}")
        module.Dispose()
    except Exception:
        # Mono.Cecil 不可用 → 回退到二进制搜索 + 文件大小
        try:
            data = mscorlib.read_bytes()
            size = len(data)
            for _, method_name in _HOOK_CRITICAL_METHODS:
                if method_name.encode() not in data:
                    missing.append(method_name)
            # 小 mscorlib + 即使名字存在也可能体被裁
            if size < _MSCORLIB_MIN_SIZE and len(missing) < 3:
                missing.append(f"mscorlib过小({size/1024/1024:.1f}MB)")
        except Exception:
            pass

    is_severe = len(missing) >= 3
    if is_severe:
        warning(
            f"检测到 BCL 被激进裁减：缺少 {len(missing)} 个 "
            f"Hook 引擎依赖方法（{', '.join(missing[:5])}...）。\n"
            "BepInEx/MonoMod/Harmony 运行时注入不可行，请改用离线资源汉化路线。"
        )
    elif missing:
        debug(f"BCL 部分裁减（{len(missing)} 个方法缺失），运行时注入可能不稳定")

    return is_severe, missing


def get_bepinex_variant(game_dir: Path) -> str | None:
    """根据 Unity 架构、位数、版本确定正确的 BepInEx 变体 key。

    返回 None 表示运行时注入不可行（旧版 Unity / 深度 stripping），
    应回退到离线资源汉化。
    """
    arch = detect_unity_arch(game_dir)
    bits = detect_unity_bits(game_dir)
    version = detect_unity_version(game_dir)
    year = parse_unity_year(version)

    if arch == UnityArchitecture.unknown:
        return None

    variant_base = f"{arch}_{bits}"

    # 阻挡 1：旧版 Unity（≤5.5）
    blocked, blocked_version = is_legacy_unity_runtime_injection_blocked(game_dir)
    if blocked:
        warning(
            f"Unity {blocked_version} 运行时注入已禁用：doorstop/BepInEx "
            "容易导致主菜单或对话点击失效，请改用离线资源汉化/备份回填方案。"
        )
        return None

    # 阻挡 2：BCL 被激进裁减（深度 stripping）
    is_stripped, _ = detect_bcl_stripping(game_dir)
    if is_stripped:
        warning("已将游戏标记为「仅离线汉化」，跳过运行时注入部署。")
        return None

    # Unity 6000 Mono：用 BepInEx 5.x，而不是 BepInEx 6 BE。
    # 原因：XUnity.AutoTranslator 没有 BepInEx 6 Mono 版插件（只有 BepInEx5-Mono 和
    # BepInEx6-IL2CPP），装在 BE6 Mono 上会 "0 plugins to load"。
    # 5.x 在 Unity6 上唯一的坑是 Preloader/MonoMod 调用了 Module.GetPEKind（精简 Mono
    # 没有此方法），由部署阶段对 Unity6+mono 打 GetPEKind 补丁解决
    # （见 BepInExDeployer._apply_unity6_5x_getpekind_patch）。
    if arch == UnityArchitecture.mono and year and year >= 6000:
        return "mono_x86" if bits == "x86" else "mono_x64"

    # 检查 variant 是否在下载源中
    if variant_base in BEPINEX_SOURCES:
        return variant_base

    return variant_base
