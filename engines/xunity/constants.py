"""XUnity/BepInEx 常量：下载源、参考工具路径、打包数据目录与版本兼容矩阵。"""

from __future__ import annotations

import os
from pathlib import Path

from core.resources import resource_path


XUAT_REDIRECT_MARKER = "\u180e"


# ---------------------------------------------------------------------------
# BepInEx 下载源（官方 GitHub）
# ---------------------------------------------------------------------------

BEPINEX_SOURCES = {
    # BepInEx 5.x — Mono Unity
    "mono_x64": {
        "url": "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.2/BepInEx_win_x64_5.4.23.2.zip",
        "arch": "x64",
    },
    "mono_x86": {
        "url": "https://github.com/BepInEx/BepInEx/releases/download/v5.4.23.2/BepInEx_win_x86_5.4.23.2.zip",
        "arch": "x86",
    },
    # BepInEx 6.x — IL2CPP Unity
    # 使用参考工具验证过的构建（含完整 dotnet .NET 6.0 运行时 + 新版 Cpp2IL），
    # 替代 GitHub 6.0.0-pre.2（metadata v31+ 兼容性未验证）。
    "il2cpp_x64": {
        "url": "bepinex_il2cpp_x64.zip",  # 本地缓存（从参考工具 trans/x64 打包）
        "arch": "x64",
    },
    "il2cpp_x86": {
        "url": "bepinex_il2cpp_x86.zip",  # 本地缓存（从参考工具 trans/x86 打包）
        "arch": "x86",
    },
    # BepInEx 6 BE — Unity 6000 Mono (BepInEx 5.x doesn't work with MonoBleedingEdge)
    "mono6000_x64": {
        "url": "https://builds.bepinex.dev/projects/bepinex_be/755/BepInEx-Unity.Mono-win-x64-6.0.0-be.755%2B3fab71a.zip",
        "arch": "x64",
        "needs_patch": True,  # UnityLogWriter internal call fix required
    },
}

# 各 BepInEx 变体在 BepInEx/core 下的入口 Preloader DLL 名称。
# 用于判断「已部署的 BepInEx 是否与当前 Unity 版本/架构匹配」：
# 例如 Unity 6000 Mono 必须是 6-BE 版（BepInEx.Unity.Mono.Preloader.dll），
# 若 core 里只有 5.x 的 BepInEx.Preloader.dll，则属于错误版本，需重新部署。
# 未列入的 variant（如 il2cpp_*）沿用「core 内有任意 dll 即视为已部署」的宽松判断。
BEPINEX_PRELOADER_ENTRY = {
    "mono_x64": "BepInEx.Preloader.dll",
    "mono_x86": "BepInEx.Preloader.dll",
    "mono6000_x64": "BepInEx.Unity.Mono.Preloader.dll",
}

# core 中属于 BepInEx 框架自身的文件前缀。重新部署时按这些前缀清理旧版本，
# 但保留诸如 XUnity.Common.dll 之类的第三方插件依赖（它们也被放在 core）。
_BEPINEX_FRAMEWORK_PREFIXES = (
    "BepInEx", "0Harmony", "HarmonyXInterop", "Mono.Cecil",
    "MonoMod", "SemanticVersioning", "AssetRipper",
)

# XUnity.AutoTranslator 下载源 — Mono 和 IL2CPP 需要不同的插件构建
# Mono：使用本地打包的 5.6.1（源码来自参考工具 trans/mono_x64）。
# 5.4.1 在 Unity6 上有 TMP hook 死循环 bug，切勿回退。
# zip 内容：BepInEx/core/XUnity.Common.dll + BepInEx/plugins/XUnity.AutoTranslator/ +
#          BepInEx/plugins/XUnity.ResourceRedirector/
XUNITY_DOWNLOAD_URLS = {
    "mono": "XUnity.AutoTranslator-mono.zip",  # 本地缓存文件（5.6.1）
    "il2cpp": (
        "https://github.com/bbepis/XUnity.AutoTranslator/releases/download/"
        "v5.6.1/XUnity.AutoTranslator-BepInEx-IL2CPP-5.6.1.zip"
    ),
}

# 参考工具路径 — 其中 trans/ 包含验证过的各变体。
# 优先级: config.ref_tool_dir > 环境变量 GT_REF_TOOL_DIR > 默认安装路径。
# 该目录仅作为打包 BepInEx/XUAT 变体的可选来源，缺失时回退远程下载。
_DEFAULT_REF_TOOL_TRANS = Path(
    "C:/games/工具/游戏一键汉化工具/游戏一键汉化工具/trans"
)


def _resolve_ref_tool_trans() -> Path:
    import os
    try:
        from config import get_config
        custom = getattr(get_config(), "ref_tool_dir", "")
        if custom:
            return Path(custom)
    except Exception:
        pass
    env = os.environ.get("GT_REF_TOOL_DIR", "")
    if env:
        return Path(env)
    return _DEFAULT_REF_TOOL_TRANS


_REF_TOOL_TRANS = _resolve_ref_tool_trans()
_REF_TOOL_MONO_X64 = _REF_TOOL_TRANS / "mono_x64"
_REF_TOOL_MONO_X86 = _REF_TOOL_TRANS / "mono_x86"
_REF_TOOL_IL2CPP_X64 = _REF_TOOL_TRANS / "x64"
_REF_TOOL_IL2CPP_X86 = _REF_TOOL_TRANS / "x86"

# 字体下载源（思源黑体 / Source Han Sans CN, SIL Open Font License）
FONT_DOWNLOAD_URL = (
    "https://github.com/adobe-fonts/source-han-sans/raw/release/SubsetOTF/CN/"
    "SourceHanSansCN-Regular.otf"
)

# 系统字体候选项。只接受明确开源/用户安装的字体，不复制 Windows 专有字体。
SYSTEM_FONT_CANDIDATES = [
    "SourceHanSansCN-Regular.otf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansHWSC-Regular.otf",
    "NotoSansCJK.ttc",
    "NotoSansCJKsc-Regular.otf",
    "NotoSansSC-Regular.otf",
    "NotoSansSC-VF.ttf",
]

# 打包数据目录 — PyInstaller exe 运行时数据解压到这里
def _get_bundled_data_dir() -> Path | None:
    """PyInstaller 打包后 sys._MEIPASS 指向解压的 data 目录。"""
    import sys
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return Path(mp) / "data"
    return None

_BUNDLED_DATA = _get_bundled_data_dir()

# 工具缓存目录。优先级: 打包数据 > TOOLS_CACHE > Downloads 回退
try:
    from core.tool_manager import get_tools_dir
    TOOLS_CACHE = get_tools_dir()
except Exception:
    TOOLS_CACHE = Path.home() / "Downloads" / ".game_translator" / "tools"

# 随工具仓库分发的预制补丁目录
if _BUNDLED_DATA:
    _PATCH_ASSET_DIR = _BUNDLED_DATA / "assets" / "bepinex_patches"
else:
    _PATCH_ASSET_DIR = resource_path("assets", "bepinex_patches")


# ---------------------------------------------------------------------------
# BepInEx 版本兼容矩阵
# ---------------------------------------------------------------------------

BEPINEX_VERSION_MATRIX = [
    # (min_unity_year, max_unity_year, arch, bepinex_version, variant_suffix)
    # variant_suffix 用于构造 BEPINEX_SOURCES key
    (2000, 5, "mono", "4.x", None),        # Unity < 5.6，不自动部署，提示手动
    (5, 2017, "mono", "5.x", ""),           # Unity 5.6–2017
    (5, 2017, "il2cpp", "5.x", ""),         # IL2CPP 早期也走 5.x
    (2018, 2021, "mono", "5.4.x", ""),      # Unity 2018–2021 Mono
    (2018, 2021, "il2cpp", "6.x", ""),      # Unity 2018–2021 IL2CPP
    (2022, 9999, "mono", "6.x", ""),        # Unity 2022+ 统一 6.x
    (2022, 9999, "il2cpp", "6.x", ""),
]
